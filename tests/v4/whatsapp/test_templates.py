"""Template sync (item 2): list the account's templates, send only approved MARKETING/UTILITY ones.

Everything runs against `httpx.MockTransport`: no network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from autonomy.channels.dispatcher import SkipReason
from autonomy.channels.whatsapp import GRAPH_URL, WhatsAppCloudAdapter, sign_body
from autonomy.channels.whatsapp_guard import WhatsAppGuard
from autonomy.channels.whatsapp_health import WhatsAppHealth
from autonomy.channels.whatsapp_templates import (
    TemplateCatalogue,
    TemplateError,
    WhatsAppTemplate,
    parse_templates,
)
from autonomy.contracts import Channel, ContentStatus, Message
from tests.v2.channels.test_dispatcher import NOW, World, decision, state

WABA = "104996122399130"
PNID = "106540352242922"
SEND_OK = {"messaging_product": "whatsapp", "messages": [{"id": "wamid.OK"}]}


def tpl(
    name: str, status: str = "APPROVED", category: str = "MARKETING", language: str = "en"
) -> dict[str, Any]:
    return {
        "id": f"id_{name}",
        "name": name,
        "language": language,
        "category": category,
        "status": status,
        "components": [{"type": "BODY", "text": "Hi {{1}}, your {{2}} is back."}],
    }


PAGE_1 = {
    "data": [
        tpl("winback_3"),
        tpl("winback_4", status="PENDING"),
        tpl("otp_code", category="AUTHENTICATION"),
    ],
    "paging": {"next": f"{GRAPH_URL}/v21.0/{WABA}/message_templates?after=abc"},
}
PAGE_2 = {"data": [tpl("order_update", category="UTILITY"), tpl("old_promo", status="REJECTED")]}


class Graph:
    """A fake Graph API: serves the template pages and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/message_templates"):
            return httpx.Response(200, json=PAGE_2 if request.url.params.get("after") else PAGE_1)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=SEND_OK)
        return httpx.Response(404, json={"error": {"code": 100, "message": "unknown path"}})


def adapter(graph: Graph, **kw: Any) -> WhatsAppCloudAdapter:
    return WhatsAppCloudAdapter(
        PNID, "token", "secret", transport=httpx.MockTransport(graph), waba_id=WABA, **kw
    )


def wa_message(template: str | None = "winback_3") -> Message:
    return Message(
        message_id="msg_1",
        decision_id="dec_1",
        customer_id="c1",
        channel=Channel.WHATSAPP,
        to="+15550001111",
        body="Hi Priya",
        template_name=template,
        template_params=("Priya", "trail shoes"),
        send_at=NOW,
    )


def test_sync_lists_every_page_and_keeps_only_approved_marketing_and_utility() -> None:
    graph = Graph()
    wa = adapter(graph)
    usable = wa.sync_templates(NOW)
    assert sorted(t.name for t in usable) == ["order_update", "winback_3"]
    assert [r.url.path for r in graph.requests] == [f"/v21.0/{WABA}/message_templates"] * 2
    first = graph.requests[0]
    assert first.url.params["fields"] == "name,language,category,status,components"
    assert first.headers["Authorization"] == "Bearer token"
    # The whole list is known, not only the usable part.
    assert {t.name for t in wa.templates.all()} == {
        "winback_3",
        "winback_4",
        "otp_code",
        "order_update",
        "old_promo",
    }
    assert wa.templates.get("winback_3", "en") is not None and wa.templates.synced_at == NOW


def test_template_fields_are_read_from_the_components() -> None:
    [first, *_] = parse_templates(PAGE_1)
    assert first.name == "winback_3" and first.parameter_count == 2 and first.body.startswith("Hi {{1}}")
    assert first.usable and first.refusal() == ""


def test_a_response_that_is_not_a_template_list_is_an_error() -> None:
    with pytest.raises(TemplateError):
        parse_templates({"oops": []})
    wa = WhatsAppCloudAdapter(
        PNID, "t", "s", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    )
    with pytest.raises(RuntimeError):
        wa.list_templates()


def test_listing_needs_the_account_id_and_reports_graph_errors() -> None:
    with pytest.raises(RuntimeError, match="waba_id"):
        WhatsAppCloudAdapter(PNID, "t", "s").list_templates()

    def denied(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 190, "message": "Invalid OAuth access token."}})

    wa = WhatsAppCloudAdapter(PNID, "t", "s", transport=httpx.MockTransport(denied), waba_id=WABA)
    with pytest.raises(RuntimeError, match="190"):
        wa.sync_templates(NOW)
    assert not wa.templates.synced  # a failed sync leaves the catalogue as it was


def test_a_next_page_off_the_graph_api_is_never_followed_with_our_token() -> None:
    def evil(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "paging": {"next": "https://evil.example/steal"}})

    wa = WhatsAppCloudAdapter(PNID, "t", "s", transport=httpx.MockTransport(evil), waba_id=WABA)
    with pytest.raises(TemplateError):
        wa.list_templates()


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("winback_4", "PENDING"),
        ("old_promo", "REJECTED"),
        ("otp_code", "AUTHENTICATION"),
        ("never_submitted", "not in the business account"),
    ],
)
def test_a_send_naming_a_template_that_is_not_usable_is_refused_before_any_request(
    name: str, why: str
) -> None:
    graph = Graph()
    wa = adapter(graph)
    wa.sync_templates(NOW)
    graph.requests.clear()
    receipt = wa.send(wa_message(name))
    assert not receipt.accepted and receipt.error is not None
    assert receipt.error.startswith("template_not_approved") and why in receipt.error
    assert graph.requests == []  # refused before dispatch: nothing reached Meta


def test_an_approved_template_is_sent_and_an_unsynced_account_changes_nothing() -> None:
    graph = Graph()
    wa = adapter(graph)
    assert wa.send(wa_message("anything_at_all")).accepted  # never synced: Meta is the only judge
    wa.sync_templates(NOW)
    assert wa.send(wa_message("winback_3")).accepted
    assert sum(r.url.path.endswith("/messages") for r in graph.requests) == 2


def test_a_template_status_webhook_pulls_a_template_back_at_once() -> None:
    graph = Graph()
    wa = adapter(graph)
    wa.sync_templates(NOW)
    assert wa.send(wa_message("winback_3")).accepted
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": WABA,
                    "time": 1_760_000_000,
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {
                                "event": "PAUSED",
                                "message_template_name": "winback_3",
                                "message_template_language": "en",
                                "reason": "NONE",
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    headers = {"X-Hub-Signature-256": sign_body("secret", body)}
    assert wa.verify_webhook(headers, body)
    assert wa.parse_webhook(headers, body) == []  # a catalogue change, not a customer event
    receipt = wa.send(wa_message("winback_3"))
    assert not receipt.accepted and "PAUSED" in (receipt.error or "")
    # ... and Meta approving it again puts it back.
    approved = body.replace(b"PAUSED", b"REINSTATED")
    wa.parse_webhook({}, approved)
    assert wa.send(wa_message("winback_3")).accepted


def test_synced_templates_become_draft_content_for_the_critic() -> None:
    catalogue = TemplateCatalogue(parse_templates(PAGE_1) + parse_templates(PAGE_2))
    drafts = catalogue.drafts(NOW, goal_id="goal_winback")
    assert sorted(d.template_name or "" for d in drafts) == ["order_update", "winback_3"]
    assert all(d.status is ContentStatus.DRAFT and d.channel is Channel.WHATSAPP for d in drafts)
    # Re-syncing gives the same ids, so a content store can upsert them.
    assert [d.content_id for d in drafts] == [
        d.content_id for d in catalogue.drafts(NOW, goal_id="goal_winback")
    ]


def test_the_dispatcher_skips_a_decision_whose_template_is_not_approved() -> None:
    w = World()
    catalogue = TemplateCatalogue(
        [WhatsAppTemplate(name="winback_3", language="en", category="MARKETING", status="APPROVED")]
    )
    guard = WhatsAppGuard(catalogue, WhatsAppHealth())
    w.dispatcher.guards = {Channel.WHATSAPP: guard}
    ok = decision(channel=Channel.WHATSAPP, content_id="ci_3")  # template winback_3
    bad = decision(channel=Channel.WHATSAPP, content_id="ci_4", offer_id="freeship")  # winback_4: not listed
    report = w.dispatcher.dispatch([ok, bad], {"c1": state()}, NOW)
    assert [r.decision_id for r in report.sent] == [ok.decision_id]
    assert report.skip_reasons() == {SkipReason.TEMPLATE_NOT_APPROVED.value: 1}
    assert len(w.whatsapp.sent) == 1  # nothing was handed to the adapter for the bad one
