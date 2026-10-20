"""`/dispatch`: send one message through the gateway, take provider callbacks, look a message up.

The routes are thin: they parse, read the clock once, call one `DispatchGateway` method and wrap
the answer. Every rule (consent, DND, quiet hours, idempotency, failover) lives in
`autonomy.channels.gateway`, so it is tested without an HTTP server.

- `POST /dispatch/send` -> 202 `{status: queued|sent|refused, reasons, ...}`. 202 even when the
  message went out at once: the answer is "accepted for processing"; delivery comes later through
  the webhooks. A repeated `idempotency_key` returns the first answer, with `replayed: true`.
- `POST /dispatch/webhooks/{provider}`: a provider's delivery reports or inbound texts; 401 when
  the signature does not match, 404 for a provider the gateway does not have.
- `GET /dispatch/messages/{idempotency_key}`: the message's record, attempts included.

The aggregator mounts `router` under `/v2/engines`. The gateway and the clock are taken from the
app's state (`attach_gateway`), as `api.v2.deps` does for the main routes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool

from autonomy.channels.consent import ConsentProof
from autonomy.channels.gateway import (
    DispatchGateway,
    GatewayError,
    MessageRecord,
    Recipient,
    SendPayload,
    SendRequest,
    SendStatus,
)
from autonomy.contracts import Channel

GATEWAY_STATE_KEY = "dispatch_gateway"
CLOCK_STATE_KEY = "dispatch_clock"

Clock = Callable[[], datetime]

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


def _system_clock() -> datetime:
    return datetime.now(UTC)


def attach_gateway(app: Starlette, gateway: DispatchGateway, clock: Clock | None = None) -> None:
    """Make `gateway` (and `clock`, the one source of "now" for these routes) what the routes use."""
    setattr(app.state, GATEWAY_STATE_KEY, gateway)
    setattr(app.state, CLOCK_STATE_KEY, clock or _system_clock)


def get_gateway(request: Request) -> DispatchGateway:
    gateway = getattr(request.app.state, GATEWAY_STATE_KEY, None)
    if not isinstance(gateway, DispatchGateway):
        raise RuntimeError("the dispatch router is mounted without a gateway; call attach_gateway(app, ...)")
    return gateway


def get_clock(request: Request) -> Clock:
    clock = getattr(request.app.state, CLOCK_STATE_KEY, None)
    return clock if callable(clock) else _system_clock


GatewayDep = Annotated[DispatchGateway, Depends(get_gateway)]
ClockDep = Annotated[Clock, Depends(get_clock)]


class SendBody(BaseModel):
    """The request. Unknown keys are an error, so a misspelt field never silently drops a rule."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    mandate_id: str | None = None
    channel: Channel
    recipient: Recipient
    payload: SendPayload
    consent_proof: ConsentProof | None = None
    decision_id: str | None = None
    send_at: AwareDatetime | None = None


class SendResponse(BaseModel):
    idempotency_key: str
    status: SendStatus
    reasons: list[str]
    message_id: str
    provider: str | None = None
    provider_message_id: str | None = None
    next_attempt_at: datetime | None = None
    replayed: bool = False


class WebhookResponse(BaseModel):
    provider: str
    events: int
    stored: int
    suppressed: int


def _response(record: MessageRecord, replayed: bool) -> SendResponse:
    return SendResponse(
        idempotency_key=record.idempotency_key,
        status=record.status,
        reasons=list(record.reasons),
        message_id=record.message_id,
        provider=record.provider,
        provider_message_id=record.provider_message_id,
        next_attempt_at=record.next_attempt_at,
        replayed=replayed,
    )


@router.post("/send", status_code=202)
async def send(body: SendBody, gateway: GatewayDep, clock: ClockDep) -> SendResponse:
    request = SendRequest(
        idempotency_key=body.idempotency_key,
        decision_id=body.decision_id,
        mandate_id=body.mandate_id,
        channel=body.channel,
        recipient=body.recipient,
        payload=body.payload,
        consent_proof=body.consent_proof,
        send_at=body.send_at,
    )
    # Provider calls block on the network, so they run off the event loop.
    outcome = await run_in_threadpool(gateway.send, request, clock())
    return _response(outcome.record, outcome.replayed)


@router.post("/webhooks/{provider}")
async def webhook(provider: str, request: Request, gateway: GatewayDep, clock: ClockDep) -> WebhookResponse:
    raw = await request.body()
    headers = dict(request.headers)
    try:
        outcome = await run_in_threadpool(gateway.handle_webhook, provider, headers, raw, clock())
    except GatewayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return WebhookResponse(
        provider=outcome.provider,
        events=len(outcome.events),
        stored=outcome.stored,
        suppressed=outcome.suppressed,
    )


@router.get("/messages/{idempotency_key}")
def message(idempotency_key: str, gateway: GatewayDep) -> MessageRecord:
    record = gateway.get(idempotency_key)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no message with key {idempotency_key!r}")
    return record
