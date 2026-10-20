"""The dispatch gateway, the SMS adapter, consent and DND registries, and rate limits. No network."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.engines.dispatch import attach_gateway, router
from autonomy.channels.consent import (
    ConsentProof,
    FileDndRegistry,
    InMemoryConsentRegistry,
    InMemoryDndRegistry,
    SqlConsentRegistry,
    evidence_hash,
    normalize_msisdn,
)
from autonomy.channels.gateway import (
    GATEWAY_DECISION_JOB,
    DispatchGateway,
    Provider,
    Recipient,
    RetryPolicy,
    SendPayload,
    SendRequest,
    SendStatus,
    pack_rules,
)
from autonomy.channels.ratelimit import Rate, RateLimiter, TokenBucket
from autonomy.channels.sms import (
    DltConfig,
    DltTemplate,
    Msg91Style,
    SmsAdapter,
    SmsEncoding,
    TwilioStyle,
    analyze,
    sign_body,
)
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionKind,
    EventType,
    GoalStatus,
    Message,
    SendReceipt,
)
from autonomy.fakes import (
    FakeChannelAdapter,
    InMemoryDecisionLog,
    InMemoryEventStore,
    InMemoryJobQueue,
    InMemoryStateStore,
    InMemoryWorkspace,
)
from autonomy.fakes.samples import sample_brief, sample_content, sample_goal
from autonomy.store.sql import memory_engine

NOW = datetime(2026, 10, 20, 6, 30, tzinfo=UTC)  # 12:00 in India, 02:30 in New York
PHONE = "+91 98123 45678"
MSISDN = "919812345678"
TEMPLATE = DltTemplate(
    template_id="1107160000000000001",
    text="Hi {#var#}, use code {#alphanumeric#} for 10% off at ACME. Reply STOP to opt out",
    header="ACMEIN",
)
DLT = DltConfig(entity_id="1101600000000000001", header="ACMEXX", templates={"winback": TEMPLATE})


class ScriptedAdapter:
    """Answers with the given receipts in turn (the last one repeats) and remembers every message."""

    def __init__(self, *receipts: SendReceipt, channel: Channel = Channel.SMS) -> None:
        self.channel = channel
        self.receipts = list(receipts) or [SendReceipt(accepted=True, provider_message_id="pm")]
        self.sent: list[Message] = []

    def send(self, message: Message) -> SendReceipt:
        self.sent.append(message)
        receipt = self.receipts[min(len(self.sent), len(self.receipts)) - 1]
        if receipt.accepted and receipt.provider_message_id == "pm":
            return SendReceipt(accepted=True, provider_message_id=f"pm_{len(self.sent)}_{id(self)}")
        return receipt

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        return True

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list:  # type: ignore[type-arg]
        return []


def ok() -> SendReceipt:
    return SendReceipt(accepted=True, provider_message_id="pm")


def err(text: str = "HTTP 503: try later") -> SendReceipt:
    return SendReceipt(accepted=False, error=text)


def proof(**kw: object) -> ConsentProof:
    base: dict[str, object] = {
        "consent_id": "cons_1",
        "purpose": "promotional",
        "channel": Channel.SMS,
        "captured_at": NOW - timedelta(days=1),
        "source": "web_form",
        "evidence_hash": evidence_hash("form post 1"),
        "recipient": PHONE,
    }
    base.update(kw)
    return ConsentProof.model_validate(base)


def request(key: str = "k1", **kw: object) -> SendRequest:
    base: dict[str, object] = {
        "idempotency_key": key,
        "channel": Channel.SMS,
        "recipient": Recipient(address=PHONE, customer_id="c1", timezone="Asia/Kolkata", country="IN"),
        "payload": SendPayload(template_name="winback", template_params=("Priya", "SAVE10")),
        "consent_proof": proof(),
    }
    base.update(kw)
    return SendRequest.model_validate(base)


def gateway(*adapters: ScriptedAdapter, **kw: object) -> DispatchGateway:
    opts: dict[str, object] = {"dnd": InMemoryDndRegistry(), "queue": InMemoryJobQueue()}
    opts.update(kw)
    providers = [Provider(f"p{i}", a) for i, a in enumerate(adapters or (ScriptedAdapter(),))]
    return DispatchGateway({Channel.SMS: providers}, **opts)  # type: ignore[arg-type]


# --- segments ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "encoding", "units", "parts"),
    [
        ("", SmsEncoding.GSM7, 0, 0),
        ("a" * 160, SmsEncoding.GSM7, 160, 1),
        ("a" * 161, SmsEncoding.GSM7, 161, 2),
        ("a" * 306, SmsEncoding.GSM7, 306, 2),
        ("a" * 307, SmsEncoding.GSM7, 307, 3),
        ("€" * 80, SmsEncoding.GSM7, 160, 1),  # extension characters take two septets
        ("€" * 81, SmsEncoding.GSM7, 162, 2),
        ("₹" * 70, SmsEncoding.UCS2, 70, 1),
        ("₹" * 71, SmsEncoding.UCS2, 71, 2),
        ("₹" * 134, SmsEncoding.UCS2, 134, 2),
        ("₹" * 135, SmsEncoding.UCS2, 135, 3),
        ("नमस्ते", SmsEncoding.UCS2, 6, 1),
        ("hi 😀", SmsEncoding.UCS2, 5, 1),  # an emoji is two UTF-16 units
    ],
)
def test_segments(text: str, encoding: SmsEncoding, units: int, parts: int) -> None:
    seg = analyze(text)
    assert (seg.encoding, seg.units, seg.count) == (encoding, units, parts)


def test_one_non_gsm_character_switches_the_whole_text_to_ucs2() -> None:
    assert analyze("a" * 100).count == 1
    assert analyze("a" * 99 + "’").count == 2  # a curly quote is not GSM-7: 100 UCS-2 units > 70


# --- the SMS adapter -----------------------------------------------------------------------------


def sms_message(**kw: object) -> Message:
    base: dict[str, object] = {
        "message_id": "msg_1",
        "decision_id": "dec_1",
        "customer_id": "c1",
        "channel": Channel.SMS,
        "to": PHONE,
        "body": "",
        "template_name": "winback",
        "template_params": ("Priya", "SAVE10"),
        "send_at": NOW,
    }
    base.update(kw)
    return Message.model_validate(base)


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response or httpx.Response(200, json={"type": "success", "message": "req_1"})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.response


def test_dlt_fields_are_injected_and_the_text_is_the_registered_template() -> None:
    rec = Recorder()
    adapter = SmsAdapter(Msg91Style("key"), dlt=DLT, transport=httpx.MockTransport(rec))
    receipt = adapter.send(sms_message())
    assert receipt.accepted and receipt.provider_message_id == "req_1"
    body = json.loads(rec.requests[0].content)
    assert body["DLT_TE_ID"] == TEMPLATE.template_id
    assert body["PE_ID"] == DLT.entity_id
    assert body["sender"] == "ACMEIN"  # the template's own header wins over the default
    assert body["sms"] == [
        {
            "message": "Hi Priya, use code SAVE10 for 10% off at ACME. Reply STOP to opt out",
            "to": [MSISDN],
        }
    ]
    assert body["unicode"] == 0
    assert rec.requests[0].headers["authkey"] == "key"


def test_twilio_style_carries_dlt_fields_as_form_params() -> None:
    rec = Recorder(httpx.Response(201, json={"sid": "SM1", "status": "queued"}))
    adapter = SmsAdapter(TwilioStyle("AC1", "tok"), dlt=DLT, transport=httpx.MockTransport(rec))
    assert adapter.send(sms_message()).provider_message_id == "SM1"
    sent = rec.requests[0]
    form = dict(x.split("=", 1) for x in sent.content.decode().split("&"))
    assert form["EntityId"] == DLT.entity_id and form["TemplateId"] == TEMPLATE.template_id
    assert form["To"] == "%2B" + MSISDN and form["From"] == "ACMEIN"
    assert sent.url.path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert sent.headers["Idempotency-Key"] == "msg_1"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        (("Priya",), "dlt_var_count"),
        (("Priya", "SAVE10", "x"), "dlt_var_count"),
        (("P" * 31, "SAVE10"), "dlt_var_too_long"),
        (("Priya", "SAVE-10"), "dlt_var_type"),
    ],
)
def test_dlt_variable_checks_refuse_before_any_network_call(params: tuple[str, ...], code: str) -> None:
    rec = Recorder()
    adapter = SmsAdapter(Msg91Style("key"), dlt=DLT, transport=httpx.MockTransport(rec))
    receipt = adapter.send(sms_message(template_params=params))
    assert not receipt.accepted and receipt.error is not None
    assert receipt.error.startswith("invalid:") and code in receipt.error
    assert rec.requests == []


def test_unregistered_template_is_refused() -> None:
    adapter = SmsAdapter(Msg91Style("key"), dlt=DLT, transport=httpx.MockTransport(Recorder()))
    receipt = adapter.send(sms_message(template_name="nope"))
    assert receipt.error is not None and "dlt_template_unknown" in receipt.error


def test_delivery_report_maps_back_to_the_decision_and_needs_a_signature() -> None:
    rec = Recorder(httpx.Response(201, json={"sid": "SM9"}))
    adapter = SmsAdapter(
        TwilioStyle("AC1", "tok"), dlt=DLT, transport=httpx.MockTransport(rec), webhook_secret="s3"
    )
    adapter.send(sms_message())
    body = b"MessageSid=SM9&MessageStatus=delivered&To=%2B919812345678"
    assert not adapter.verify_webhook({}, body)
    assert adapter.verify_webhook({"x-signature": sign_body("s3", body)}, body)
    [event] = adapter.parse_webhook({"X-Autonomy-Received-At": NOW.isoformat()}, body)
    assert event.type is EventType.DELIVER and event.decision_id == "dec_1" and event.ts == NOW
    assert event.customer_id == "c1"


def test_msg91_reports_and_ndnc_block() -> None:
    rec = Recorder()
    adapter = SmsAdapter(Msg91Style("key"), dlt=DLT, transport=httpx.MockTransport(rec))
    adapter.send(sms_message())
    body = json.dumps(
        [
            {"requestId": "req_1", "number": MSISDN, "status": "1", "date": "2026-10-20 12:05:00"},
            {"requestId": "req_1", "number": MSISDN, "status": "9", "desc": "NDNC"},
            {"requestId": "unknown", "number": MSISDN, "status": "1"},
        ]
    ).encode()
    events = adapter.parse_webhook({"X-Autonomy-Received-At": NOW.isoformat()}, body)
    assert [e.type for e in events] == [EventType.DELIVER, EventType.BLOCK]
    assert events[0].ts == datetime(2026, 10, 20, 6, 35, tzinfo=UTC)  # IST report time


def test_stop_keyword_becomes_unsubscribe() -> None:
    adapter = SmsAdapter(TwilioStyle("AC1", "tok"), sender="+15550001111")
    body = b"MessageSid=SM2&From=%2B919812345678&Body=stop+please"
    [event] = adapter.parse_webhook({"X-Autonomy-Received-At": NOW.isoformat()}, body)
    assert event.type is EventType.UNSUBSCRIBE and event.attrs["address"] == MSISDN
    assert event.text is None
    [reply] = adapter.parse_webhook(
        {"X-Autonomy-Received-At": NOW.isoformat()}, b"MessageSid=SM3&From=%2B919812345678&Body=when%3F"
    )
    assert reply.type is EventType.REPLY and reply.text == "when?"


# --- consent and DND -----------------------------------------------------------------------------


def test_missing_consent_is_refused() -> None:
    adapter = ScriptedAdapter()
    gw = gateway(adapter)
    out = gw.send(request(consent_proof=None), NOW)
    assert out.status is SendStatus.REFUSED and "consent_missing" in out.reasons
    assert adapter.sent == []


def test_consent_for_another_purpose_or_channel_is_refused() -> None:
    gw = gateway()
    assert (
        "consent_purpose_mismatch"
        in gw.send(request("a", consent_proof=proof(purpose="service")), NOW).reasons
    )
    assert (
        "consent_channel_mismatch"
        in gw.send(request("b", consent_proof=proof(channel=Channel.WHATSAPP)), NOW).reasons
    )


def test_registry_must_know_the_proof_and_revocation_wins() -> None:
    registry = InMemoryConsentRegistry()
    gw = gateway(consent=registry)
    assert "consent_unknown" in gw.send(request("a"), NOW).reasons
    registry.record(proof())
    assert gw.send(request("b"), NOW).status is SendStatus.SENT
    assert (
        "consent_evidence_mismatch"
        in gw.send(request("c", consent_proof=proof(evidence_hash="x")), NOW).reasons
    )
    registry.revoke("cons_1", NOW)
    assert "consent_revoked" in gw.send(request("d"), NOW).reasons


def test_transactional_messages_need_no_marketing_consent_or_dnd_check() -> None:
    gw = gateway(dnd=InMemoryDndRegistry([PHONE]))
    payload = SendPayload(template_name="winback", template_params=("a", "b"), purpose="transactional")
    assert gw.send(request(payload=payload, consent_proof=None), NOW).status is SendStatus.SENT


def test_dnd_number_is_refused_for_promotional_sms(tmp_path: Path) -> None:
    path = tmp_path / "ncpr.txt"
    path.write_text("# scrub list\n09812345678\n919800000000,1;3\n", encoding="utf-8")
    dnd = FileDndRegistry(path)
    assert len(dnd) == 2
    adapter = ScriptedAdapter()
    gw = gateway(adapter, dnd=dnd)
    out = gw.send(request(), NOW)
    assert out.status is SendStatus.REFUSED and out.reasons == ("dnd_registered",)
    assert adapter.sent == []
    assert dnd.blocks("+91 98000 00000", "3") and not dnd.blocks("919800000000", "5")


def test_india_promotional_sms_without_a_dnd_registry_fails_closed() -> None:
    gw = gateway(dnd=None)
    assert gw.send(request(), NOW).reasons == ("dnd_registry_unavailable",)


def test_india_sms_needs_a_dlt_template() -> None:
    gw = gateway()
    out = gw.send(request(payload=SendPayload(body="Hello")), NOW)
    assert out.reasons == ("dlt_template_missing",)


def test_mandate_required_when_configured() -> None:
    gw = gateway(
        require_mandate=True, mandate_check=lambda m, _r, _n: None if m == "man_1" else "mandate_invalid"
    )
    assert gw.send(request("a"), NOW).reasons == ("mandate_missing",)
    assert gw.send(request("b", mandate_id="man_x"), NOW).reasons == ("mandate_invalid",)
    assert gw.send(request("c", mandate_id="man_1"), NOW).status is SendStatus.SENT


def test_sql_consent_registry_round_trip() -> None:
    registry = SqlConsentRegistry(memory_engine())
    registry.record(proof())
    assert registry.get("cons_1") == proof()
    assert registry.find(Channel.SMS, MSISDN, "promotional", NOW) == proof()
    assert registry.find(Channel.SMS, MSISDN, "service", NOW) is None
    assert registry.revoke_recipient(Channel.SMS, MSISDN, NOW) == 1
    assert registry.find(Channel.SMS, MSISDN, "promotional", NOW) is None
    stored = registry.get("cons_1")
    assert stored is not None and stored.revoked_at == NOW


# --- idempotency, failover, retries --------------------------------------------------------------


def test_idempotent_resend_returns_the_first_result() -> None:
    adapter = ScriptedAdapter()
    gw = gateway(adapter)
    first = gw.send(request(), NOW)
    again = gw.send(request(payload=SendPayload(template_name="winback", template_params=("X", "Y"))), NOW)
    assert first.status is SendStatus.SENT and not first.replayed
    assert again.replayed and again.record == first.record
    assert len(adapter.sent) == 1


def test_failover_on_provider_error_without_double_send() -> None:
    primary, backup = ScriptedAdapter(err()), ScriptedAdapter()
    gw = gateway(primary, backup)
    out = gw.send(request(), NOW)
    assert out.status is SendStatus.SENT and out.record.provider == "p1"
    assert [a.provider for a in out.record.attempts] == ["p0", "p1"]
    assert len(primary.sent) == 1 and len(backup.sent) == 1
    assert primary.sent[0].message_id == backup.sent[0].message_id
    gw.run_due(NOW + timedelta(hours=1))
    assert len(primary.sent) == 1 and len(backup.sent) == 1  # nothing left to send


def test_ambiguous_error_never_fails_over_and_retries_the_same_provider() -> None:
    primary, backup = ScriptedAdapter(err("ReadTimeout: timed out"), ok()), ScriptedAdapter()
    gw = gateway(primary, backup)
    out = gw.send(request(), NOW)
    assert out.status is SendStatus.QUEUED and out.reasons[0] == "ambiguous_retry"
    assert backup.sent == []
    assert out.record.next_attempt_at is not None
    gw.run_due(out.record.next_attempt_at)
    record = gw.get("k1")
    assert record is not None and record.status is SendStatus.SENT and record.provider == "p0"
    assert backup.sent == [] and len(primary.sent) == 2


def test_invalid_message_is_neither_retried_nor_failed_over() -> None:
    primary, backup = ScriptedAdapter(err("invalid: dlt_var_count: 2 slots, got 1")), ScriptedAdapter()
    gw = gateway(primary, backup)
    out = gw.send(request(), NOW)
    assert out.status is SendStatus.REFUSED and out.reasons[0] == "invalid_message"
    assert backup.sent == []


def test_buffered_retry_with_backoff_then_failure() -> None:
    events = InMemoryEventStore()
    adapter = ScriptedAdapter(err())
    gw = gateway(
        adapter, retry=RetryPolicy(base=timedelta(seconds=30), max_rounds=3, jitter=0), events=events
    )
    out = gw.send(request(), NOW)
    assert out.status is SendStatus.QUEUED and out.record.next_attempt_at == NOW + timedelta(seconds=30)
    gw.run_due(NOW + timedelta(seconds=30))
    record = gw.get("k1")
    assert record is not None and record.next_attempt_at == NOW + timedelta(seconds=90)  # 30s, then 60s
    gw.run_due(NOW + timedelta(seconds=90))
    record = gw.get("k1")
    assert record is not None and record.status is SendStatus.FAILED and len(adapter.sent) == 3
    assert [e.type for e in events.scan()] == [EventType.FAIL]


def test_stale_job_sends_nothing() -> None:
    queue = InMemoryJobQueue()
    adapter = ScriptedAdapter(err(), ok())
    gw = gateway(adapter, queue=queue)
    out = gw.send(request(), NOW)
    assert out.record.job_id is not None
    job = queue.get(out.record.job_id)
    assert job is not None
    later = NOW + timedelta(hours=1)
    assert gw.run_job(job, later) is not None
    assert gw.run_job(job, later) is not None  # the same job again
    assert len(adapter.sent) == 2


# --- rate limits ---------------------------------------------------------------------------------


def test_token_bucket_is_deterministic() -> None:
    bucket = TokenBucket(Rate(2, timedelta(seconds=10)))
    assert bucket.take(NOW) and bucket.take(NOW) and not bucket.take(NOW)
    assert bucket.wait(NOW) == timedelta(seconds=5, microseconds=1)
    assert bucket.take(NOW + timedelta(seconds=5, microseconds=1))
    assert not bucket.take(NOW - timedelta(hours=1))  # a clock going back grants nothing


def test_provider_rate_limit_queues_until_a_token_is_there() -> None:
    adapter = ScriptedAdapter()
    limiter = RateLimiter({("p0", Channel.SMS): Rate(1, timedelta(minutes=1))})
    gw = gateway(adapter, limiter=limiter)
    assert gw.send(request("a"), NOW).status is SendStatus.SENT
    second = gw.send(request("b"), NOW)
    assert second.status is SendStatus.QUEUED and second.reasons == ("rate_limited_provider",)
    assert second.record.next_attempt_at is not None
    gw.run_due(second.record.next_attempt_at)
    record = gw.get("b")
    assert record is not None and record.status is SendStatus.SENT and len(adapter.sent) == 2


def test_full_provider_bucket_spills_to_the_next_provider() -> None:
    primary, backup = ScriptedAdapter(), ScriptedAdapter()
    limiter = RateLimiter({("p0", Channel.SMS): Rate(1, timedelta(minutes=1))})
    gw = gateway(primary, backup, limiter=limiter)
    gw.send(request("a"), NOW)
    out = gw.send(request("b"), NOW)
    assert out.status is SendStatus.SENT and out.record.provider == "p1"


def test_recipient_rate_limit() -> None:
    gw = gateway(limiter=RateLimiter(recipient_rate=Rate(1, timedelta(hours=1))))
    assert gw.send(request("a"), NOW).status is SendStatus.SENT
    out = gw.send(request("b"), NOW)
    assert out.reasons == ("rate_limited_recipient",)
    assert out.record.next_attempt_at == NOW + timedelta(hours=1, microseconds=1)


# --- quiet hours ---------------------------------------------------------------------------------


def test_quiet_hours_in_the_recipients_zone_queue_the_message() -> None:
    adapter = ScriptedAdapter()
    gw = gateway(adapter)
    late = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)  # 22:30 in India
    out = gw.send(request(), late)
    assert out.status is SendStatus.QUEUED and out.reasons == ("quiet_hours",)
    assert out.record.next_attempt_at == datetime(2026, 10, 21, 3, 30, tzinfo=UTC)  # 09:00 IST
    assert adapter.sent == []
    gw.run_due(datetime(2026, 10, 21, 3, 30, tzinfo=UTC))
    record = gw.get("k1")
    assert record is not None and record.status is SendStatus.SENT and len(adapter.sent) == 1


def test_us_quiet_hours_tcpa_and_unknown_zone_is_conservative() -> None:
    gw = gateway()
    us = Recipient(address="+1 415 555 0100", customer_id="u1", timezone="America/Los_Angeles")
    us_proof = proof(recipient="+14155550100")
    body = SendPayload(body="20% off today. Reply STOP to opt out")
    at = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)  # 07:30 in Los Angeles, before 8am
    out = gw.send(request("u", recipient=us, payload=body, consent_proof=us_proof), at)
    assert out.reasons == ("quiet_hours",) and out.record.country == "US"
    assert out.record.next_attempt_at == datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # 08:00 PDT
    # No zone known: 14:30 UTC is 10:30 in New York but 04:30 in Honolulu, so it waits for 08:00 there.
    nozone = us.model_copy(update={"timezone": None, "customer_id": "u2"})
    out2 = gw.send(request("v", recipient=nozone, payload=body, consent_proof=us_proof), at)
    assert out2.reasons == ("quiet_hours",)
    assert out2.record.next_attempt_at == datetime(2026, 10, 20, 18, 0, tzinfo=UTC)  # 08:00 HST


def test_quiet_hours_without_a_queue_refuse() -> None:
    gw = gateway(queue=None)
    out = gw.send(request(), datetime(2026, 10, 20, 17, 0, tzinfo=UTC))
    assert out.status is SendStatus.REFUSED and out.reasons == ("quiet_hours", "no_queue")


# --- STOP through the webhook --------------------------------------------------------------------


def test_stop_suppresses_the_number_and_revokes_consent() -> None:
    registry = InMemoryConsentRegistry([proof()])
    sms = SmsAdapter(TwilioStyle("AC1", "tok"), dlt=DLT, webhook_secret="s3")
    gw = DispatchGateway(
        {Channel.SMS: [Provider("twilio", sms)]},
        consent=registry,
        dnd=InMemoryDndRegistry(),
        events=InMemoryEventStore(),
    )
    body = b"MessageSid=SM2&From=%2B919812345678&Body=STOP"
    outcome = gw.handle_webhook("twilio", {"X-Signature": sign_body("s3", body)}, body, NOW)
    assert outcome.suppressed == 1 and outcome.stored == 1
    out = gw.send(request(), NOW + timedelta(minutes=1))
    assert out.status is SendStatus.REFUSED
    assert "opted_out" in out.reasons and "consent_revoked" in out.reasons


# --- the API -------------------------------------------------------------------------------------


def test_api_send_get_and_webhooks() -> None:
    sms = SmsAdapter(TwilioStyle("AC1", "tok"), dlt=DLT, webhook_secret="s3")
    adapter = ScriptedAdapter()
    gw = DispatchGateway(
        {Channel.SMS: [Provider("primary", adapter), Provider("twilio", sms)]},
        dnd=InMemoryDndRegistry(),
        queue=InMemoryJobQueue(),
    )
    app = FastAPI()
    app.include_router(router, prefix="/v2/engines")
    attach_gateway(app, gw, clock=lambda: NOW)
    client = TestClient(app)
    body = request().model_dump(mode="json", exclude_none=True)
    first = client.post("/v2/engines/dispatch/send", json=body)
    assert first.status_code == 202 and first.json()["status"] == "sent"
    again = client.post("/v2/engines/dispatch/send", json=body)
    assert again.status_code == 202 and again.json()["replayed"] is True
    assert len(adapter.sent) == 1
    refused = client.post(
        "/v2/engines/dispatch/send", json={**body, "idempotency_key": "k2", "consent_proof": None}
    )
    assert refused.json()["status"] == "refused" and refused.json()["reasons"] == ["consent_missing"]
    got = client.get("/v2/engines/dispatch/messages/k1")
    assert got.status_code == 200 and got.json()["attempts"][0]["provider"] == "primary"
    assert client.get("/v2/engines/dispatch/messages/nope").status_code == 404
    assert client.post("/v2/engines/dispatch/webhooks/nope", content=b"").status_code == 404
    hook = b"MessageSid=SM2&From=%2B919812345678&Body=STOP"
    assert client.post("/v2/engines/dispatch/webhooks/twilio", content=hook).status_code == 401
    signed = client.post(
        "/v2/engines/dispatch/webhooks/twilio", content=hook, headers={"X-Signature": sign_body("s3", hook)}
    )
    assert signed.status_code == 200 and signed.json()["suppressed"] == 1
    bad = client.post("/v2/engines/dispatch/send", json={**body, "idempotency_key": "k3", "extra": 1})
    assert bad.status_code == 422


# --- the decision path ---------------------------------------------------------------------------


def test_decision_path_defers_quiet_hours_and_fails_over() -> None:
    ws = InMemoryWorkspace()
    goal = sample_goal(NOW).model_copy(
        update={"status": GoalStatus.ACTIVE, "autonomy": AutonomyLevel.AUTOPILOT}
    )
    ws.put_goal(goal)
    ws.put_brief(sample_brief(NOW))
    ws.put_content(sample_content(NOW))
    events, queue = InMemoryEventStore(), InMemoryJobQueue()
    bad, good = ScriptedAdapter(err(), channel=Channel.EMAIL), FakeChannelAdapter(Channel.EMAIL)
    gw = DispatchGateway({Channel.EMAIL: [Provider("ses", bad), Provider("backup", good)]}, queue=queue)
    gw.attach_dispatcher(lambda cid, _ch: f"{cid}@example.test", ws, events)
    state = CustomerState(
        customer_id="c1",
        as_of=NOW,
        timezone="Asia/Kolkata",
        top_categories=("tea",),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )
    late = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)  # 22:30 in Kolkata
    decision = Decision(
        decision_id="dec_1",
        customer_id="c1",
        made_at=late,
        kind=DecisionKind.ACT,
        action=Action(goal_id="goal_winback", channel=Channel.EMAIL, content_id="ci_0", send_at=late),
        probability=0.9,
        reason="test",
        policy_version="t",
    )
    report = gw.dispatch([decision], {"c1": state}, late)
    [result] = report.results
    assert result.status.value == "queued" and result.reason == "quiet_hours"
    log, states = InMemoryDecisionLog(), InMemoryStateStore()
    log.record([decision])
    states.put([state])
    morning = datetime(2026, 10, 21, 3, 30, tzinfo=UTC)
    job = queue.claim([GATEWAY_DECISION_JOB], worker="t", now=morning)
    assert job is not None
    report = gw.run_decision_job(job, log, states, morning)
    assert [r.status.value for r in report.results] == ["sent"]
    assert len(bad.sent) == 1 and len(good.sent) == 1
    assert [e.type for e in events.scan()] == [EventType.SEND]


def _decision_setup(
    *adapters: ScriptedAdapter,
) -> tuple[DispatchGateway, InMemoryJobQueue, InMemoryEventStore, Decision, CustomerState]:
    ws = InMemoryWorkspace()
    goal = sample_goal(NOW).model_copy(
        update={"status": GoalStatus.ACTIVE, "autonomy": AutonomyLevel.AUTOPILOT}
    )
    ws.put_goal(goal)
    ws.put_brief(sample_brief(NOW))
    ws.put_content(sample_content(NOW))
    events, queue = InMemoryEventStore(), InMemoryJobQueue()
    providers = [Provider(f"e{i}", a) for i, a in enumerate(adapters)]
    gw = DispatchGateway({Channel.EMAIL: providers}, queue=queue, events=events)
    gw.attach_dispatcher(lambda cid, _ch: f"{cid}@example.test", ws, events)
    state = CustomerState(
        customer_id="c1",
        as_of=NOW,
        timezone="Asia/Kolkata",
        top_categories=("tea",),
        channels={Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    )
    decision = Decision(
        decision_id="dec_1",
        customer_id="c1",
        made_at=NOW,
        kind=DecisionKind.ACT,
        action=Action(goal_id="goal_winback", channel=Channel.EMAIL, content_id="ci_0", send_at=NOW),
        probability=0.9,
        reason="test",
        policy_version="t",
    )
    return gw, queue, events, decision, state


def test_decision_path_does_not_retry_an_invalid_message() -> None:
    bad = ScriptedAdapter(err("invalid: the body is empty"), channel=Channel.EMAIL)
    backup = ScriptedAdapter(channel=Channel.EMAIL)
    gw, queue, _events, decision, state = _decision_setup(bad, backup)
    [result] = gw.dispatch([decision], {"c1": state}, NOW).results
    assert result.status.value == "failed" and result.reason.startswith("invalid:")
    assert backup.sent == []
    assert queue.claim([GATEWAY_DECISION_JOB], worker="t", now=NOW + timedelta(days=1)) is None


def test_a_decision_sent_once_is_not_sent_again_under_another_key() -> None:
    adapter = ScriptedAdapter(channel=Channel.EMAIL)
    gw, _queue, _events, decision, state = _decision_setup(adapter)
    assert [r.status.value for r in gw.dispatch([decision], {"c1": state}, NOW).results] == ["sent"]
    again = SendRequest(
        idempotency_key="other",
        decision_id="dec_1",
        channel=Channel.EMAIL,
        recipient=Recipient(address="c1@example.test", customer_id="c1"),
        payload=SendPayload(body="Hello, unsubscribe here", purpose="transactional"),
    )
    out = gw.send(again, NOW)
    assert out.status is SendStatus.REFUSED and out.reasons == ("already_sent",)
    assert len(adapter.sent) == 1


def test_numbers_are_sent_international_and_never_guessed() -> None:
    rec = Recorder()
    sms = SmsAdapter(Msg91Style("k"), sender="ACME", transport=httpx.MockTransport(rec))
    gw = DispatchGateway({Channel.SMS: [Provider("m", sms)]}, queue=InMemoryJobQueue())
    body = SendPayload(body="hi")
    # A 10-digit international number (Singapore) must not be re-read as an Indian national one.
    sg = Recipient(address="+65 9123 4567", customer_id="s", timezone="Asia/Singapore")
    out = gw.send(
        request("sg", recipient=sg, payload=body, consent_proof=proof(recipient="+6591234567")), NOW
    )
    assert out.status is SendStatus.SENT
    assert json.loads(rec.requests[-1].content)["sms"][0]["to"] == ["6591234567"]
    # A UK national number takes the UK code, not the default Indian one.
    gb = Recipient(address="07700 900123", customer_id="g", timezone="Europe/London", country="GB")
    gb_proof = proof(recipient="+447700900123")
    out = gw.send(request("gb", recipient=gb, payload=body, consent_proof=gb_proof), NOW)
    assert out.status is SendStatus.SENT and out.record.address == "447700900123"
    # A national number for a country whose format we do not know is refused, not guessed.
    fr = Recipient(address="06 12 34 56 78", customer_id="f", timezone="Europe/Paris", country="FR")
    out = gw.send(request("fr", recipient=fr, payload=body, consent_proof=proof(recipient=None)), NOW)
    assert out.status is SendStatus.REFUSED and out.reasons == ("no_address",)
    assert len(rec.requests) == 2


def test_msisdn_normalisation() -> None:
    assert normalize_msisdn("+91 98123-45678") == MSISDN
    assert normalize_msisdn("09812345678") == MSISDN
    assert normalize_msisdn("9812345678") == MSISDN
    assert normalize_msisdn("0091 9812345678") == MSISDN


# --- rules from the compliance packs -------------------------------------------------------------


def test_pack_rules_drive_the_gate() -> None:
    rules = pack_rules()
    india = rules("IN", Channel.SMS)
    assert india.dlt_required and Channel.SMS in india.dnd_channels
    assert (india.quiet_start_hour, india.quiet_end_hour) == (21, 9)
    assert "transactional" in india.consent_exempt_purposes
    gw = gateway(rules=rules, dnd={"IN": InMemoryDndRegistry([PHONE])})
    assert gw.send(request("a"), NOW).reasons == ("dnd_registered",)
    gw = gateway(rules=rules, dnd={"IN": InMemoryDndRegistry()})
    assert gw.send(request("b"), NOW).status is SendStatus.SENT
    late = gw.send(request("c"), datetime(2026, 10, 20, 17, 0, tzinfo=UTC))
    assert late.reasons == ("quiet_hours",)
    us = Recipient(address="+1 415 555 0100", customer_id="u1", timezone="America/New_York")
    out = gw.send(
        request(
            "d", recipient=us, payload=SendPayload(body="Hi"), consent_proof=proof(recipient="+14155550100")
        ),
        datetime(2026, 10, 20, 16, 0, tzinfo=UTC),
    )
    assert "dnd_registry_unavailable" in out.reasons  # the US pack asks for a DNC lookup; none given


def test_dlt_config_from_pack_registrations() -> None:
    """The adapter fills the same registered text the India pack checks, keyed by template id."""
    from autonomy.compliance_packs.base import DltRegistration, Purpose

    entity = "1101600000000000001"
    regs = (
        DltRegistration(
            template_id="1107160000000000011",
            header="123456",
            entity_id=entity,
            text="Hi {#var#}, your code is {#numeric#}.",
            purpose=Purpose.PROMOTIONAL,
        ),
        DltRegistration(
            template_id="1107160000000000012",
            header="ACMEXX",
            entity_id=entity,
            text="Order {#alphanumeric#} shipped.",
        ),
    )
    config = DltConfig.from_registrations(regs)
    assert config.entity_id == entity and config.header == "123456"
    assert config.templates["1107160000000000012"].header == "ACMEXX"
    assert config.templates["1107160000000000011"].var_max_chars == 30
    with pytest.raises(ValueError):
        DltConfig.from_registrations(())
    with pytest.raises(ValueError):
        DltConfig.from_registrations(
            (regs[0], regs[1].model_copy(update={"entity_id": "1101600000000000009"}))
        )
