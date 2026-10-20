"""The real channel adapters against recorded provider payloads: no network anywhere.

SES is driven through a recording stub client, WhatsApp and the webhook tool through
`httpx.MockTransport`, and every webhook fixture in `fixtures/` is parsed.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from autonomy.channels.client_tool import (
    CSV_COLUMNS,
    SIGNATURE_HEADER,
    CsvExportAdapter,
    WebhookToolAdapter,
    decision_payload,
    sign,
)
from autonomy.channels.ses import (
    SesEmailAdapter,
    decode_tag,
    encode_tag,
    sns_string_to_sign,
    trusted_sns_url,
)
from autonomy.channels.whatsapp import WhatsAppCloudAdapter, decode_opaque, encode_opaque, sign_body
from autonomy.contracts import Channel, EventType, Message, stable_id
from autonomy.interfaces import ChannelAdapter

FIXTURES = Path(__file__).parent / "fixtures"
DEC = "dec_5c0f3a9b7e2d41c8a6f0"
CUST = "cust_000123"
MSG = "msg_8b1e4d2c9a7f60e3b5d1"
WAMID = "wamid.HBgLMTY1MDU1NTEyMzQVAgARGBI5QTNDQTVCM0Q0Q0Q2RTY3RTcA"
PNID = "106540352242922"
SEND_AT = datetime(2026, 10, 19, 9, 14, tzinfo=UTC)
DISPATCHER_SEND_ID = stable_id("ev", MSG, EventType.SEND.value)
"""The id `Dispatcher._send` gives the SEND it records for an accepted message."""


def fixture(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


def message(channel: Channel, **overrides: Any) -> Message:
    base: dict[str, Any] = {
        "message_id": MSG,
        "decision_id": DEC,
        "customer_id": CUST,
        "channel": channel,
        "to": "jane@example.com",
        "subject": "Your favourites are back",
        "body": "Hi Jane, the shoes you liked are back in stock.",
        "send_at": SEND_AT,
    }
    base.update(overrides)
    return Message(**base)


# --- SES -----------------------------------------------------------------------------------------


class RecordingSes:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response if response is not None else {"MessageId": "0100019a-test-000000"}
        self.error = error

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def ses_adapter(client: RecordingSes | None = None, **kwargs: Any) -> SesEmailAdapter:
    kwargs.setdefault("verifier", lambda doc: True)
    return SesEmailAdapter(
        client or RecordingSes(), "Acme Store <news@mail.acme-store.example>", "autonomy-events", **kwargs
    )


def test_adapters_satisfy_the_channel_adapter_protocol(tmp_path: Path) -> None:
    adapters = [
        ses_adapter(),
        WhatsAppCloudAdapter(PNID, "token", "secret"),
        WebhookToolAdapter("https://hooks.example/flow", "s3cret"),
        CsvExportAdapter(tmp_path / "out.csv"),
    ]
    for adapter in adapters:
        assert isinstance(adapter, ChannelAdapter)
    assert [a.channel for a in adapters] == [
        Channel.EMAIL,
        Channel.WHATSAPP,
        Channel.WEBHOOK,
        Channel.WEBHOOK,
    ]


def test_ses_send_builds_a_sesv2_request_with_tags_and_unsubscribe_headers() -> None:
    client = RecordingSes()
    adapter = ses_adapter(
        client, unsubscribe_url="https://acme-store.example/u?d={decision_id}&c={customer_id}"
    )
    receipt = adapter.send(message(Channel.EMAIL))
    assert receipt.accepted and receipt.provider_message_id == "0100019a-test-000000"
    (call,) = client.calls
    assert call["FromEmailAddress"] == "Acme Store <news@mail.acme-store.example>"
    assert call["Destination"] == {"ToAddresses": ["jane@example.com"]}
    assert call["ConfigurationSetName"] == "autonomy-events"
    simple = call["Content"]["Simple"]
    assert simple["Subject"] == {"Data": "Your favourites are back", "Charset": "UTF-8"}
    assert simple["Body"]["Text"]["Data"].startswith("Hi Jane")
    headers = {h["Name"]: h["Value"] for h in simple["Headers"]}
    assert headers["List-Unsubscribe"] == (
        f"<mailto:news@mail.acme-store.example?subject=unsubscribe%20{MSG}>, "
        f"<https://acme-store.example/u?d={DEC}&c={CUST}>"
    )
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert call["EmailTags"] == [
        {"Name": "decision_id", "Value": DEC},
        {"Name": "customer_id", "Value": CUST},
        {"Name": "message_id", "Value": MSG},
    ]


def test_ses_mailto_only_unsubscribe_has_no_one_click_header() -> None:
    request = ses_adapter().build_request(message(Channel.EMAIL))
    names = [h["Name"] for h in request["Content"]["Simple"]["Headers"]]
    assert names == ["List-Unsubscribe"]


def test_ses_tags_survive_ids_that_are_not_tag_safe() -> None:
    name, value = encode_tag("customer_id", "jane.doe@example.com")
    assert name == "customer_id_b32" and value.isalnum() and value.islower()
    assert decode_tag({name: [value]}, "customer_id") == "jane.doe@example.com"
    assert encode_tag("decision_id", DEC) == ("decision_id", DEC)
    request = ses_adapter().build_request(message(Channel.EMAIL, customer_id="jane.doe@example.com"))
    assert {"Name": "customer_id_b32", "Value": value} in request["EmailTags"]


def test_ses_send_refusals_never_raise() -> None:
    assert not ses_adapter().send(message(Channel.EMAIL, subject=None)).accepted
    assert not ses_adapter().send(message(Channel.WHATSAPP)).accepted
    failing = ses_adapter(RecordingSes(error=RuntimeError("Throttling: Maximum sending rate exceeded")))
    receipt = failing.send(message(Channel.EMAIL))
    assert not receipt.accepted and receipt.error is not None and "Throttling" in receipt.error
    assert not ses_adapter(RecordingSes(response={})).send(message(Channel.EMAIL)).accepted


@pytest.mark.parametrize(
    ("name", "event_type"),
    [
        ("send", EventType.SEND),
        ("delivery", EventType.DELIVER),
        ("bounce", EventType.BOUNCE),
        ("complaint", EventType.COMPLAINT),
        ("open", EventType.OPEN),
        ("click", EventType.CLICK),
        ("subscription", EventType.UNSUBSCRIBE),
        ("reject", EventType.FAIL),
    ],
)
def test_ses_every_event_type_is_parsed_with_its_decision(name: str, event_type: EventType) -> None:
    adapter = ses_adapter()
    body = fixture("ses", f"{name}.json")
    assert adapter.verify_webhook({"x-amz-sns-message-type": "Notification"}, body)
    (event,) = adapter.parse_webhook({}, body)
    assert event.type is event_type
    assert event.decision_id == DEC
    assert event.channel is Channel.EMAIL and event.source == "ses"
    assert event.ts.tzinfo is not None
    assert event.attrs["message_id"] == MSG
    assert str(event.attrs["provider_message_id"]).startswith("0100019a")
    expected_customer = "jane.doe@example.com" if name == "click" else CUST
    assert event.customer_id == expected_customer
    # SNS retries the same notification: the event id must not change.
    assert adapter.parse_webhook({}, body)[0].event_id == event.event_id


def test_provider_send_confirmations_share_the_dispatchers_send_id() -> None:
    """The dispatcher records a SEND on an accepted send; the provider's own SEND must dedupe onto it."""
    (ses_send,) = ses_adapter().parse_webhook({}, fixture("ses", "send.json"))
    (wa_send,) = WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook(
        {}, fixture("whatsapp", "status_sent.json")
    )
    assert ses_send.event_id == wa_send.event_id == DISPATCHER_SEND_ID
    (delivery,) = ses_adapter().parse_webhook({}, fixture("ses", "delivery.json"))
    assert delivery.event_id != DISPATCHER_SEND_ID


def test_ses_event_details_are_kept() -> None:
    adapter = ses_adapter()
    (bounce,) = adapter.parse_webhook({}, fixture("ses", "bounce.json"))
    assert bounce.attrs["permanent"] is True and bounce.attrs["bounce_type"] == "Permanent"
    assert bounce.ts == datetime(2026, 10, 19, 9, 14, 6, 101000, tzinfo=UTC)
    (click,) = adapter.parse_webhook({}, fixture("ses", "click.json"))
    assert click.attrs["link"] == "https://acme-store.example/new-in?utm_source=autonomy"
    (complaint,) = adapter.parse_webhook({}, fixture("ses", "complaint.json"))
    assert complaint.attrs["complaint_type"] == "abuse"


def test_ses_topic_preferences_become_preference_events() -> None:
    events = ses_adapter().parse_webhook({}, fixture("ses", "subscription_topic.json"))
    assert [e.type for e in events] == [EventType.PREFERENCE, EventType.PREFERENCE]
    assert [(e.attrs["key"], e.attrs["value"]) for e in events] == [
        ("email_topic:offers", "OptOut"),
        ("email_topic:new_arrivals", "OptIn"),
    ]
    assert len({e.event_id for e in events}) == 2


def test_ses_delivery_delay_and_untagged_mail_carry_no_events() -> None:
    adapter = ses_adapter()
    assert adapter.parse_webhook({}, fixture("ses", "delivery_delay.json")) == []
    doc = json.loads(fixture("ses", "delivery.json"))
    record = json.loads(doc["Message"])
    record["mail"]["tags"] = {"ses:configuration-set": ["autonomy-events"]}
    doc["Message"] = json.dumps(record)
    assert adapter.parse_webhook({}, json.dumps(doc).encode()) == []


def test_ses_subscription_confirmation_exposes_the_subscribe_url() -> None:
    adapter = ses_adapter()
    body = fixture("ses", "subscription_confirmation.json")
    assert adapter.verify_webhook({"x-amz-sns-message-type": "SubscriptionConfirmation"}, body)
    assert adapter.parse_webhook({}, body) == []
    url = adapter.subscribe_url(body)
    assert url is not None and url.startswith(
        "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription"
    )
    assert adapter.subscribe_url(fixture("ses", "delivery.json")) is None
    forged = json.loads(body)
    forged["SubscribeURL"] = "https://attacker.example/?Action=ConfirmSubscription"
    assert adapter.subscribe_url(json.dumps(forged).encode()) is None


def test_ses_verify_delegates_the_signature_and_checks_the_envelope() -> None:
    body = fixture("ses", "delivery.json")
    seen: list[str] = []

    def verifier(doc: Any) -> bool:
        seen.append(sns_string_to_sign(doc))
        return True

    assert ses_adapter(verifier=verifier).verify_webhook({}, body)
    doc = json.loads(body)
    assert seen == [
        f"Message\n{doc['Message']}\nMessageId\n{doc['MessageId']}\nSubject\n{doc['Subject']}\n"
        f"Timestamp\n{doc['Timestamp']}\nTopicArn\n{doc['TopicArn']}\nType\nNotification\n"
    ]
    # fails closed: no verifier, a verifier that says no, or one that blows up
    assert not SesEmailAdapter(RecordingSes(), "a@b.example", "cs").verify_webhook({}, body)
    assert not ses_adapter(verifier=lambda doc: False).verify_webhook({}, body)

    def broken(doc: Any) -> bool:
        raise OSError("cannot fetch the signing cert")

    assert not ses_adapter(verifier=broken).verify_webhook({}, body)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d: d.update(SigningCertURL="https://evil.example/cert.pem"),
        lambda d: d.update(SigningCertURL="http://sns.eu-west-1.amazonaws.com/cert.pem"),
        lambda d: d.update(SignatureVersion="3"),
        lambda d: d.pop("Signature"),
        lambda d: d.update(Type="Surprise"),
        lambda d: d.update(TopicArn="arn:aws:sns:eu-west-1:999999999999:someone-else"),
    ],
)
def test_ses_verify_rejects_a_bad_envelope(tamper: Any) -> None:
    doc = json.loads(fixture("ses", "delivery.json"))
    tamper(doc)
    adapter = ses_adapter(topic_arns=["arn:aws:sns:eu-west-1:123456789012:autonomy-ses-events"])
    assert not adapter.verify_webhook({}, json.dumps(doc).encode())


def test_ses_verify_rejects_junk_and_a_mismatched_type_header() -> None:
    adapter = ses_adapter()
    assert not adapter.verify_webhook({}, b"not json")
    assert not adapter.verify_webhook({}, b"[1, 2]")
    body = fixture("ses", "delivery.json")
    assert not adapter.verify_webhook({"X-Amz-Sns-Message-Type": "SubscriptionConfirmation"}, body)
    with pytest.raises(ValueError):
        adapter.parse_webhook({}, b"not json")


def test_trusted_sns_url() -> None:
    assert trusted_sns_url("https://sns.us-east-1.amazonaws.com/x.pem")
    assert trusted_sns_url("https://sns.cn-north-1.amazonaws.com.cn/x.pem")
    assert not trusted_sns_url("https://sns.us-east-1.amazonaws.com.evil.example/x.pem")
    assert not trusted_sns_url("https://evil.example/sns.us-east-1.amazonaws.com")


# --- WhatsApp ------------------------------------------------------------------------------------


def wa_message(**overrides: Any) -> Message:
    fields: dict[str, Any] = {
        "to": "+1 (650) 555-1234",
        "subject": None,
        "template_name": "winback_offer",
        "template_params": ("Jane", "running shoes"),
    }
    fields.update(overrides)
    return message(Channel.WHATSAPP, **fields)


def test_whatsapp_payload_is_a_template_message_with_our_callback_data() -> None:
    adapter = WhatsAppCloudAdapter(PNID, "token", "secret", language="en_GB")
    payload = adapter.build_payload(wa_message(asset_url="https://cdn.example/shoe.jpg"))
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == "16505551234"
    assert payload["type"] == "template"
    template = payload["template"]
    assert template["name"] == "winback_offer"
    assert template["language"] == {"code": "en_GB"}
    assert template["components"] == [
        {
            "type": "header",
            "parameters": [{"type": "image", "image": {"link": "https://cdn.example/shoe.jpg"}}],
        },
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Jane"}, {"type": "text", "text": "running shoes"}],
        },
    ]
    assert decode_opaque(payload["biz_opaque_callback_data"]) == (DEC, CUST, MSG)
    assert adapter.messages_url == f"https://graph.facebook.com/v21.0/{PNID}/messages"


def test_whatsapp_callback_data_fits_the_limit() -> None:
    assert len(encode_opaque(DEC, CUST, "m" * 600)) <= 512
    assert decode_opaque(encode_opaque(DEC, CUST, "m" * 600)) == (DEC, CUST, None)
    with pytest.raises(ValueError):
        encode_opaque(DEC, "c" * 600, MSG)
    assert decode_opaque("someone else's data") == (None, None, None)


def test_whatsapp_send_posts_and_returns_the_wamid() -> None:
    httpx = pytest.importorskip("httpx")
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx.Response(200, content=fixture("whatsapp", "send_response.json"))

    adapter = WhatsAppCloudAdapter(PNID, "token-123", "secret", transport=httpx.MockTransport(handler))
    receipt = adapter.send(wa_message())
    assert receipt.accepted and receipt.provider_message_id == WAMID
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"https://graph.facebook.com/v21.0/{PNID}/messages"
    assert request.headers["authorization"] == "Bearer token-123"
    assert json.loads(request.content)["template"]["name"] == "winback_offer"
    adapter.close()


def test_whatsapp_send_failures_are_refused_receipts() -> None:
    httpx = pytest.importorskip("httpx")

    def error(request: Any) -> Any:
        return httpx.Response(400, content=fixture("whatsapp", "send_error.json"))

    def down(request: Any) -> Any:
        raise httpx.ConnectError("connection refused", request=request)

    receipt = WhatsAppCloudAdapter(PNID, "t", "s", transport=httpx.MockTransport(error)).send(wa_message())
    assert receipt.error == "(#132001) Template name does not exist in the translation"
    receipt = WhatsAppCloudAdapter(PNID, "t", "s", transport=httpx.MockTransport(down)).send(wa_message())
    assert not receipt.accepted and receipt.error is not None and "ConnectError" in receipt.error


def test_whatsapp_messages_without_a_template_are_refused_before_any_network() -> None:
    adapter = WhatsAppCloudAdapter(PNID, "t", "s")  # no transport: a network call would fail the test
    assert not adapter.send(wa_message(template_name=None)).accepted
    assert not adapter.send(wa_message(to="no digits")).accepted
    assert not adapter.send(message(Channel.EMAIL)).accepted


def test_whatsapp_signature_verification() -> None:
    adapter = WhatsAppCloudAdapter(PNID, "token", "app-secret")
    body = fixture("whatsapp", "status_delivered.json")
    good = sign_body("app-secret", body)
    assert adapter.verify_webhook({"X-Hub-Signature-256": good}, body)
    assert adapter.verify_webhook({"x-hub-signature-256": good}, body)
    assert not adapter.verify_webhook({"X-Hub-Signature-256": sign_body("other", body)}, body)
    assert not adapter.verify_webhook({"X-Hub-Signature-256": good}, body + b" ")
    assert not adapter.verify_webhook({}, body)
    assert not WhatsAppCloudAdapter(PNID, "token", "").verify_webhook({"X-Hub-Signature-256": good}, body)


def test_whatsapp_subscription_challenge() -> None:
    adapter = WhatsAppCloudAdapter(PNID, "token", "secret", verify_token="my-verify-token")
    params = {"hub.mode": "subscribe", "hub.verify_token": "my-verify-token", "hub.challenge": "1158201444"}
    assert adapter.verify_subscription(params) == "1158201444"
    assert adapter.verify_subscription({**params, "hub.verify_token": "wrong"}) is None
    assert adapter.verify_subscription({**params, "hub.mode": "unsubscribe"}) is None
    assert WhatsAppCloudAdapter(PNID, "token", "secret").verify_subscription(params) is None


@pytest.mark.parametrize(
    ("name", "event_type"),
    [
        ("status_sent", EventType.SEND),
        ("status_delivered", EventType.DELIVER),
        ("status_read", EventType.OPEN),
        ("status_failed", EventType.FAIL),
        ("status_failed_opted_out", EventType.BLOCK),
    ],
)
def test_whatsapp_statuses_map_to_events_with_their_decision(name: str, event_type: EventType) -> None:
    (event,) = WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook({}, fixture("whatsapp", f"{name}.json"))
    assert event.type is event_type
    assert event.decision_id == DEC and event.customer_id == CUST
    assert event.channel is Channel.WHATSAPP and event.source == "whatsapp"
    assert event.attrs["provider_message_id"] == WAMID
    assert event.attrs["message_id"] == MSG
    if event_type in (EventType.FAIL, EventType.BLOCK):
        assert event.attrs["error_code"] in (131026, 131050)


def test_whatsapp_statuses_of_messages_we_did_not_send_are_ignored() -> None:
    doc = json.loads(fixture("whatsapp", "status_delivered.json"))
    del doc["entry"][0]["changes"][0]["value"]["statuses"][0]["biz_opaque_callback_data"]
    body = json.dumps(doc).encode()
    assert WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook({}, body) == []
    traced = WhatsAppCloudAdapter(
        PNID, "t", "s", decision_lookup={WAMID: DEC}.get, customer_lookup={"16505551234": CUST}.get
    )
    (event,) = traced.parse_webhook({}, body)
    assert (event.type, event.decision_id, event.customer_id) == (EventType.DELIVER, DEC, CUST)


def test_whatsapp_status_times_are_unix_seconds() -> None:
    (event,) = WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook({}, fixture("whatsapp", "status_read.json"))
    assert event.ts == datetime.fromtimestamp(1760866211, tz=UTC)
    assert event.attrs["pricing_category"] == "marketing"


def test_whatsapp_replies_use_the_lookups() -> None:
    adapter = WhatsAppCloudAdapter(
        PNID,
        "t",
        "s",
        customer_lookup={"16505551234": CUST}.get,
        decision_lookup={WAMID: DEC}.get,
    )
    (text,) = adapter.parse_webhook({}, fixture("whatsapp", "message_text.json"))
    assert text.type is EventType.REPLY
    assert text.text == "Yes please, do you have it in blue?"
    assert text.customer_id == CUST and text.decision_id == DEC
    assert text.attrs["context_message_id"] == WAMID and text.attrs["message_type"] == "text"
    (button,) = adapter.parse_webhook({}, fixture("whatsapp", "message_button.json"))
    assert button.type is EventType.REPLY and button.text == "Stop promotions"
    assert button.event_id != text.event_id


def test_whatsapp_reply_without_lookups_falls_back_to_the_wa_id() -> None:
    (reply,) = WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook(
        {}, fixture("whatsapp", "message_text.json")
    )
    assert reply.customer_id == "16505551234" and reply.decision_id is None
    assert reply.attrs["wa_id"] == "16505551234"


def test_whatsapp_ignores_other_numbers_and_other_objects() -> None:
    other = WhatsAppCloudAdapter("999999999999999", "t", "s")
    assert other.parse_webhook({}, fixture("whatsapp", "status_sent.json")) == []
    assert WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook({}, b'{"object": "page", "entry": []}') == []
    with pytest.raises(ValueError):
        other.parse_webhook({}, b"[]")


# --- the client's own tool -----------------------------------------------------------------------


def test_webhook_tool_signs_a_deterministic_payload() -> None:
    adapter = WebhookToolAdapter(
        "https://hooks.example/flow", "s3cret", headers={"Authorization": "Klaviyo-API-Key k"}
    )
    body, headers = adapter.build_request(message(Channel.WEBHOOK))
    assert body == decision_payload(message(Channel.WEBHOOK))
    data = json.loads(body)
    assert data["schema"] == "autonomy.decision.v1"
    assert (data["decision_id"], data["customer_id"], data["message_id"]) == (DEC, CUST, MSG)
    assert data["send_at"] == "2026-10-19T09:14:00Z"
    assert headers[SIGNATURE_HEADER] == sign("s3cret", body)
    assert headers["Idempotency-Key"] == MSG
    assert headers["Authorization"] == "Klaviyo-API-Key k"
    with pytest.raises(ValueError):
        WebhookToolAdapter("https://hooks.example/flow", "")


def test_webhook_tool_send() -> None:
    httpx = pytest.importorskip("httpx")
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx.Response(202, json={"id": "klaviyo-evt-1"})

    adapter = WebhookToolAdapter(
        "https://hooks.example/flow", "s3cret", transport=httpx.MockTransport(handler)
    )
    receipt = adapter.send(message(Channel.WEBHOOK))
    assert receipt.accepted and receipt.provider_message_id == "klaviyo-evt-1"
    (request,) = seen
    assert request.headers[SIGNATURE_HEADER] == sign("s3cret", request.content)

    def refuse(request: Any) -> Any:
        return httpx.Response(500, text="flow paused")

    failing = WebhookToolAdapter(
        "https://hooks.example/flow", "s3cret", transport=httpx.MockTransport(refuse)
    )
    receipt = failing.send(message(Channel.WEBHOOK))
    assert not receipt.accepted and receipt.error == "HTTP 500: flow paused"
    # a misconfigured URL is a refusal too, not an exception into the dispatcher
    receipt = WebhookToolAdapter("not a url", "s3cret", transport=httpx.MockTransport(handler)).send(
        message(Channel.WEBHOOK)
    )
    assert not receipt.accepted and receipt.error is not None


def test_webhook_tool_callbacks_are_verified_and_parsed() -> None:
    adapter = WebhookToolAdapter("https://hooks.example/flow", "s3cret", source="klaviyo")
    body = json.dumps(
        {
            "events": [
                {
                    "type": "deliver",
                    "customer_id": CUST,
                    "decision_id": DEC,
                    "message_id": MSG,
                    "ts": "2026-10-19T09:20:00Z",
                },
                {"type": "click", "customer_id": CUST, "decision_id": DEC, "ts": "2026-10-19T10:00:00+00:00"},
            ]
        }
    ).encode()
    assert adapter.verify_webhook({SIGNATURE_HEADER.lower(): sign("s3cret", body)}, body)
    assert not adapter.verify_webhook({SIGNATURE_HEADER: sign("wrong", body)}, body)
    assert not adapter.verify_webhook({}, body)
    deliver, click = adapter.parse_webhook({}, body)
    assert (deliver.type, click.type) == (EventType.DELIVER, EventType.CLICK)
    assert deliver.channel is Channel.WEBHOOK and deliver.source == "klaviyo"
    assert deliver.decision_id == DEC and deliver.attrs["message_id"] == MSG
    assert deliver.event_id == adapter.parse_webhook({}, body)[0].event_id
    sent = json.dumps(
        {"type": "send", "customer_id": CUST, "message_id": MSG, "ts": "2026-10-19T09:15:00Z"}
    ).encode()
    assert adapter.parse_webhook({}, sent)[0].event_id == DISPATCHER_SEND_ID
    for bad in (
        b"nope",
        b'{"type": "purchase", "customer_id": "c", "ts": "2026-10-19T09:20:00Z"}',
        b'{"type": "deliver"}',
    ):
        with pytest.raises(ValueError):
            adapter.parse_webhook({}, bad)


def test_csv_export_appends_rows_with_a_header(tmp_path: Path) -> None:
    path = tmp_path / "exports" / "decisions.csv"
    adapter = CsvExportAdapter(path)
    first = adapter.send(message(Channel.WEBHOOK, template_params=("Jane", "shoes")))
    second = adapter.send(message(Channel.WEBHOOK, message_id="msg_2", body='=HYPERLINK("x")'))
    assert first.accepted and first.provider_message_id == MSG and second.accepted
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert len(rows) == 3
    record = dict(zip(CSV_COLUMNS, rows[1], strict=True))
    assert record["decision_id"] == DEC and record["template_params"] == '["Jane", "shoes"]'
    assert dict(zip(CSV_COLUMNS, rows[2], strict=True))["body"] == '\'=HYPERLINK("x")'
    assert adapter.row(message(Channel.WEBHOOK, to="+44 20 7946 0958"))[4] == "+44 20 7946 0958"
    assert adapter.row(message(Channel.WEBHOOK, to="+1+cmd|' /C calc'!A0"))[4].startswith("'+1")
    assert adapter.parse_webhook({}, b"") == [] and not adapter.verify_webhook({}, b"")


def test_csv_export_file_errors_are_refusals(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    receipt = CsvExportAdapter(blocker / "decisions.csv").send(message(Channel.WEBHOOK))
    assert not receipt.accepted and receipt.error is not None
