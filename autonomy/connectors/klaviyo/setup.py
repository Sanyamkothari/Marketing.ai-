"""The receiving side of "decide here, send there", built in the customer's Klaviyo account by API.

Autonomy hands each decided message to Klaviyo as an event of the custom metric
`Autonomy Decision` on the person's profile, carrying the rendered subject and body, the offer and
discount code, the decision id and the intended send time. A Klaviyo **flow triggered by that metric**
sends it with a template that reads those properties (`{{ event.subject }}`, `{{ event.body }}`), so
the shop's unsubscribes, suppressions, sender reputation and footer all stay in Klaviyo.

`ensure_send_setup` makes that flow exist, idempotently, with no person involved:

1. **The metric.** A custom metric exists once an event of it does. If `Autonomy Decision` is not
   there yet, one setup event is created with `backfill: true` ("recorded but does NOT trigger
   flows", per the Create Event docs) on a profile that has only an external id (no email or phone,
   so it can never be messaged or billed as active). Klaviyo processes events asynchronously, so
   setup reports `pending` and finishes on the next check.
2. **The template** `Autonomy · decision email` (a CODE template; found by name, else created).
3. **One flow per channel** (`Autonomy · email`, `Autonomy · SMS`): trigger = the metric with a
   trigger filter `channel equals <channel>`, one send action that is `live`, Smart Sending off
   (Autonomy already decides how often each person hears from the shop; frequency caps elsewhere
   would silently drop decided sends), SMS quiet hours on. Found by name first. A flow found in
   `draft` (Autonomy created it but could not switch it on) is set live; a flow a person set to
   `manual` is left alone and reported, because turning it back on is their call.

Where Klaviyo refuses part of it (missing scope, no SMS in the account, no default sender, an
account that cannot create live flows by API) the result is `guided`, with the exact steps for the
one-time setup in Klaviyo's UI. The send adapter refuses to hand anything over until the channel is
`ready`, so nothing is ever sent into a flow that does not exist.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from autonomy.connectors.base import AccessDenied, RemoteError
from autonomy.connectors.klaviyo.api import AUTONOMY_FLOW_PREFIX, DECISION_METRIC, parse_klaviyo_time
from autonomy.connectors.klaviyo.client import KlaviyoClient
from autonomy.connectors.klaviyo.mapping import SETUP_PROFILE
from autonomy.contracts import Channel

__all__ = [
    "EMAIL_FLOW",
    "EMAIL_TEMPLATE",
    "EMAIL_TEMPLATE_HTML",
    "SETUP_PROFILE_EXTERNAL_ID",
    "SMS_FLOW",
    "ChannelSetup",
    "SetupStatus",
    "ensure_send_setup",
    "find_metric_ids",
    "flow_definition",
    "guided_steps",
]

EMAIL_FLOW: Final[str] = f"{AUTONOMY_FLOW_PREFIX} email"
SMS_FLOW: Final[str] = f"{AUTONOMY_FLOW_PREFIX} SMS"
EMAIL_TEMPLATE: Final[str] = f"{AUTONOMY_FLOW_PREFIX} decision email"
FLOW_NAMES: Final[Mapping[Channel, str]] = {Channel.EMAIL: EMAIL_FLOW, Channel.SMS: SMS_FLOW}
SETUP_PROFILE_EXTERNAL_ID: Final[str] = SETUP_PROFILE
SETUP_EVENT_ID: Final[str] = "autonomy-setup-metric"
SETUP_EVENT_RETRY_ID: Final[str] = "autonomy-setup-metric-2"
SETUP_RETRY_AFTER: Final[timedelta] = timedelta(minutes=30)
ENTRY_ACTION: Final[str] = "autonomy-send"

EMAIL_TEMPLATE_HTML: Final[str] = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="max-width:600px;margin:0 auto;padding:24px;font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:1.5;color:#1f1f1f;">
{{ event.body|linebreaks }}
{% if event.discount_code %}<p style="font-size:18px;letter-spacing:1px;"><strong>{{ event.discount_code }}</strong></p>{% endif %}
</div>
<div style="max-width:600px;margin:0 auto;padding:12px 24px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#6b6b6b;">
{% unsubscribe 'Unsubscribe' %}<br>{{ organization.name }}, {{ organization.full_address }}
</div>
</body>
</html>
"""
"""Reads the rendered body (and discount code) from the decision event; Klaviyo adds the footer data."""

EMAIL_TEMPLATE_TEXT: Final[str] = (
    "{{ event.body }}\n\n{% if event.discount_code %}{{ event.discount_code }}\n\n{% endif %}"
    "Unsubscribe: {% unsubscribe_link %}\n{{ organization.name }}, {{ organization.full_address }}\n"
)

SetupState = Literal["ready", "pending", "guided"]


class ChannelSetup(BaseModel):
    """One channel's receiving flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: Channel
    ready: bool
    flow_name: str
    flow_id: str | None = None
    flow_status: str | None = None
    template_id: str | None = None
    created: bool = Field(default=False, description="Created (or switched on) by this check")
    detail: str | None = Field(default=None, description="Why it is not ready, in plain words")
    steps: tuple[str, ...] = Field(
        default=(), description="The one-time guided setup, when the API could not"
    )


class SetupStatus(BaseModel):
    """What Klaviyo's account has for Autonomy's sends, as of `checked_at`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str | None
    state: SetupState
    metric_id: str | None = None
    channels: tuple[ChannelSetup, ...] = ()
    detail: str | None = None
    checked_at: datetime

    def channel(self, channel: Channel) -> ChannelSetup | None:
        return next((c for c in self.channels if c.channel is channel), None)

    def ready_for(self, channel: Channel) -> bool:
        found = self.channel(channel)
        return found is not None and found.ready

    def flow_ids(self) -> frozenset[str]:
        return frozenset(c.flow_id for c in self.channels if c.flow_id)

    def steps(self) -> tuple[str, ...]:
        return tuple(step for c in self.channels for step in c.steps)

    def fingerprint(self) -> str:
        """Stable for the same problem, so the one alert about it is raised once (its alert id)."""
        parts = [self.state, *(f"{c.channel.value}:{c.ready}:{c.detail}" for c in self.channels)]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


# --- the check ------------------------------------------------------------------------------------


def ensure_send_setup(
    client: KlaviyoClient,
    *,
    account: Mapping[str, Any],
    channels: Sequence[Channel],
    config: Mapping[str, str],
    now: datetime,
) -> SetupStatus:
    """Find or create everything a decided message needs to be sent by Klaviyo. Never raises for a
    refusal of Klaviyo's (that becomes `guided`); a failure to reach Klaviyo at all propagates, so
    the caller retries later. `NeedsReconnect` propagates too: the connection itself is gone."""
    account_id = _text(account.get("id"))
    wanted = tuple(dict.fromkeys(c for c in channels if c in FLOW_NAMES))
    try:
        metric_ids = find_metric_ids(client, DECISION_METRIC)
        if not metric_ids:
            _create_setup_event(client, now)
            return SetupStatus(
                account_id=account_id,
                state="pending",
                channels=tuple(
                    ChannelSetup(
                        channel=c,
                        ready=False,
                        flow_name=FLOW_NAMES[c],
                        detail=f"Klaviyo is creating the {DECISION_METRIC!r} metric; setup finishes "
                        "on the next check (a few minutes)",
                    )
                    for c in wanted
                ),
                detail="waiting for Klaviyo to create the decision metric",
                checked_at=now,
            )
    except AccessDenied as exc:
        return _all_guided(
            account_id, wanted, now, f"the connection may not read or write metrics ({exc.code})"
        )
    metric_id = metric_ids[0]
    results = [
        _ensure_channel(client, channel, metric_id=metric_id, account=account, config=config)
        for channel in wanted
    ]
    state: SetupState = "ready" if all(r.ready for r in results) else "guided"
    return SetupStatus(
        account_id=account_id, state=state, metric_id=metric_id, channels=tuple(results), checked_at=now
    )


def _ensure_channel(
    client: KlaviyoClient,
    channel: Channel,
    *,
    metric_id: str,
    account: Mapping[str, Any],
    config: Mapping[str, str],
) -> ChannelSetup:
    name = FLOW_NAMES[channel]
    try:
        flows = _find_flows(client, name)
        if flows:
            return _existing(client, channel, _keep_one(client, flows))
        template_id: str | None = None
        sender: tuple[str, str] | None = None
        if channel is Channel.EMAIL:
            sender = _sender(account, config)
            if sender is None:
                return _guided(
                    channel,
                    "Klaviyo has no default sender address for this account",
                    metric_ready=True,
                )
            template_id = _ensure_template(client)
        definition = flow_definition(channel, metric_id, template_id=template_id, sender=sender)
        created = client.post(
            "flows",
            "flows:create",
            {"data": {"type": "flow", "attributes": {"name": name, "definition": definition}}},
        )
        data = _mapping(created.get("data"))
        flow_id = _text(data.get("id"))
        status = (_text(_mapping(data.get("attributes")).get("status")) or "").lower()
        if flow_id is None:
            return _guided(channel, "Klaviyo did not return the new flow's id", metric_ready=True)
        if status != "live":
            _set_live(client, flow_id)
            status = "live"
        return ChannelSetup(
            channel=channel,
            ready=True,
            flow_name=name,
            flow_id=flow_id,
            flow_status=status,
            template_id=template_id,
            created=True,
        )
    except AccessDenied as exc:
        return _guided(
            channel, f"the connection lacks a permission Klaviyo requires ({exc.code})", metric_ready=True
        )
    except RemoteError as exc:
        if exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 409, 429):
            return _guided(channel, f"Klaviyo refused to create it: {exc.message}", metric_ready=True)
        raise


def _existing(client: KlaviyoClient, channel: Channel, flow: Mapping[str, Any]) -> ChannelSetup:
    flow_id = _text(flow.get("id"))
    attributes = _mapping(flow.get("attributes"))
    status = (_text(attributes.get("status")) or "").lower()
    name = FLOW_NAMES[channel]
    if flow_id is not None and status == "draft":
        _set_live(client, flow_id)  # ours, created but never switched on (an earlier PATCH failed)
        return ChannelSetup(
            channel=channel, ready=True, flow_name=name, flow_id=flow_id, flow_status="live", created=True
        )
    if status == "live":
        return ChannelSetup(channel=channel, ready=True, flow_name=name, flow_id=flow_id, flow_status=status)
    detail = f"the flow {name!r} exists in Klaviyo but is {status or 'not live'}"
    return ChannelSetup(
        channel=channel,
        ready=False,
        flow_name=name,
        flow_id=flow_id,
        flow_status=status or None,
        detail=detail,
        steps=(
            f"In Klaviyo, open Flows and find {name!r}. Someone set it to {status or 'off'}, so Autonomy "
            "has not switched it back on.",
            "If Autonomy should send through it again, set the flow (and its message) to Live. Until then "
            f"Autonomy holds its {channel.value} decisions instead of sending them.",
        ),
    )


def _set_live(client: KlaviyoClient, flow_id: str) -> None:
    client.patch(
        f"flows/{flow_id}",
        "flows:update",
        {"data": {"type": "flow", "id": flow_id, "attributes": {"status": "live"}}},
    )


def flow_definition(
    channel: Channel,
    metric_id: str,
    *,
    template_id: str | None = None,
    sender: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The Create Flow `definition`: metric trigger filtered to this channel, one live send action."""
    trigger = {
        "type": "metric",
        "id": metric_id,
        "trigger_filter": {
            "condition_groups": [
                {
                    "conditions": [
                        {
                            "type": "metric-property",
                            "metric_id": metric_id,
                            "field": "channel",
                            "filter": {"type": "string", "operator": "equals", "value": channel.value},
                        }
                    ]
                }
            ]
        },
    }
    if channel is Channel.EMAIL:
        if template_id is None or sender is None:
            raise ValueError("an email flow needs a template and a sender")
        from_email, from_label = sender
        action: dict[str, Any] = {
            "temporary_id": ENTRY_ACTION,
            "type": "send-email",
            "data": {
                "status": "live",
                "message": {
                    "name": f"{AUTONOMY_FLOW_PREFIX} decision email",
                    "from_email": from_email,
                    "from_label": from_label,
                    "subject_line": "{{ event.subject }}",
                    "preview_text": "{{ event.preview_text|default:'' }}",
                    "template_id": template_id,
                    "smart_sending_enabled": False,
                    "transactional": False,
                    "add_tracking_params": False,
                },
            },
        }
    else:
        action = {
            "temporary_id": ENTRY_ACTION,
            "type": "send-sms",
            "data": {
                "status": "live",
                "message": {
                    "name": f"{AUTONOMY_FLOW_PREFIX} decision SMS",
                    "body": "{{ event.body }}",
                    "smart_sending_enabled": False,
                    "sms_quiet_hours_enabled": True,
                    "add_org_prefix": True,
                    "add_opt_out_language": True,
                    "shorten_links": True,
                    "transactional": False,
                },
            },
        }
    return {
        "triggers": [trigger],
        "actions": [action],
        "entry_action_id": ENTRY_ACTION,
        # Every decision is a new entry: per the spec, unit 'alltime' with duration 0 allows re-entry
        # (duration 1 would let each person through once, ever, and silently drop every later decision).
        "reentry_criteria": {"duration": 0, "unit": "alltime"},
    }


# --- lookups --------------------------------------------------------------------------------------


def find_metric_ids(client: KlaviyoClient, name: str) -> list[str]:
    """Ids of the metrics called `name` (case-insensitive). Klaviyo's metric filter covers only the
    integration, so the (short) list is read and matched here."""
    wanted = name.casefold()
    found: list[str] = []
    for item in client.collect("metrics", "metrics", {"fields[metric]": "name,integration"}):
        metric_id = _text(item.get("id"))
        metric_name = _text(_mapping(item.get("attributes")).get("name")) or ""
        if metric_id and metric_name.casefold() == wanted:
            found.append(metric_id)
    return sorted(found)


def _keep_one(client: KlaviyoClient, flows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Two live flows with our name would each send every decision (a create that timed out after
    Klaviyo stored it, or two checks racing). Keep one, the first live by id, and set every other
    live copy back to draft, so each decision is sent once."""
    live = [f for f in flows if (_text(_mapping(f.get("attributes")).get("status")) or "").lower() == "live"]
    keep = live[0] if live else flows[0]
    for extra in live:
        extra_id = _text(extra.get("id"))
        if extra is not keep and extra_id is not None:
            client.patch(
                f"flows/{extra_id}",
                "flows:update",
                {"data": {"type": "flow", "id": extra_id, "attributes": {"status": "draft"}}},
            )
    return keep


def _find_flows(client: KlaviyoClient, name: str) -> list[Mapping[str, Any]]:
    items = client.collect(
        "flows",
        "flows:read",
        {
            "filter": f'and(equals(name,"{name}"),equals(archived,false))',
            "fields[flow]": "name,status,archived,trigger_type",
        },
        max_pages=2,
    )
    exact = [i for i in items if _text(_mapping(i.get("attributes")).get("name")) == name]
    return sorted(exact, key=lambda i: _text(i.get("id")) or "")


def _ensure_template(client: KlaviyoClient) -> str:
    items = client.collect(
        "templates", "templates", {"filter": f'equals(name,"{EMAIL_TEMPLATE}")', "fields[template]": "name"}
    )
    ids = sorted(
        t
        for i in items
        if _text(_mapping(i.get("attributes")).get("name")) == EMAIL_TEMPLATE and (t := _text(i.get("id")))
    )
    if ids:
        return ids[0]
    created = client.post(
        "templates",
        "templates",
        {
            "data": {
                "type": "template",
                "attributes": {
                    "name": EMAIL_TEMPLATE,
                    "editor_type": "CODE",
                    "html": EMAIL_TEMPLATE_HTML,
                    "text": EMAIL_TEMPLATE_TEXT,
                },
            }
        },
    )
    template_id = _text(_mapping(created.get("data")).get("id"))
    if template_id is None:
        raise RemoteError("Klaviyo did not return the new template's id", status=400)
    return template_id


def _create_setup_event(client: KlaviyoClient, now: datetime) -> None:
    """Make Klaviyo create the decision metric. First a `backfill` event (it triggers no flow). If
    Klaviyo processed that one long ago (the setup profile is older than `SETUP_RETRY_AFTER`) and
    the metric is still not listed, a backfill event does not create a metric in this account, so
    a plain one is sent: no flow can trigger on a metric that does not exist yet, and the setup
    profile has no email or phone, so nothing can be sent either way. Each is recorded once (its own
    unique_id), so setup cannot stay `pending` for ever on an undocumented detail."""
    created = _setup_profile_created(client)
    processed = created is not None and now - created >= SETUP_RETRY_AFTER
    attributes: dict[str, Any] = {
        "properties": {"autonomy_setup": True, "channel": "setup"},
        "metric": {"data": {"type": "metric", "attributes": {"name": DECISION_METRIC}}},
        "profile": {"data": {"type": "profile", "attributes": {"external_id": SETUP_PROFILE_EXTERNAL_ID}}},
        "unique_id": SETUP_EVENT_RETRY_ID if processed else SETUP_EVENT_ID,
    }
    if not processed:
        attributes["backfill"] = True
    client.post(
        "events",
        "events:write",
        {"data": {"type": "event", "attributes": attributes}},
        retry_safe=True,  # unique_id: Klaviyo records the first one only
    )


def _setup_profile_created(client: KlaviyoClient) -> datetime | None:
    """When Klaviyo made the setup profile (so processed the first setup event), or None."""
    doc = client.get(
        "profiles",
        "profiles",
        {
            "filter": f'equals(external_id,"{SETUP_PROFILE_EXTERNAL_ID}")',
            "fields[profile]": "external_id,created",
        },
    )
    data = doc.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
        return None
    created = parse_klaviyo_time(_mapping(data[0].get("attributes")).get("created"))
    return created if created is not None else datetime.min.replace(tzinfo=UTC)


def _sender(account: Mapping[str, Any], config: Mapping[str, str]) -> tuple[str, str] | None:
    info = _mapping(_mapping(account.get("attributes")).get("contact_information"))
    email = (config.get("from_email") or "").strip() or _text(info.get("default_sender_email"))
    label = (
        (config.get("from_label") or "").strip()
        or _text(info.get("default_sender_name"))
        or _text(info.get("organization_name"))
    )
    if not email:
        return None
    return email, label or email


# --- guided setup -----------------------------------------------------------------------------------


def guided_steps(channel: Channel, *, metric_ready: bool) -> tuple[str, ...]:
    """The exact one-time steps in Klaviyo's UI for a channel whose flow the API could not create."""
    name = FLOW_NAMES[channel]
    steps = [
        f"In Klaviyo, open Flows → Create flow → Build your own, and name it {name!r} (exactly).",
        f"Trigger: choose Metric → {DECISION_METRIC!r}. Add a trigger filter: 'channel' equals "
        f"'{channel.value}'.",
    ]
    if not metric_ready:
        steps.append(
            f"If {DECISION_METRIC!r} is not in the list yet, wait ten minutes: Autonomy has already sent the "
            "event that creates it."
        )
    if channel is Channel.EMAIL:
        steps += [
            f"Add an Email action. Template: {EMAIL_TEMPLATE!r} (Autonomy created it in Templates; if it is "
            "missing, create a code template whose body is {{ event.body|linebreaks }} with your footer). "
            "Subject line: {{ event.subject }}. Sender: your usual sender.",
            "In the email's settings turn Smart Sending off: Autonomy already decides how often each person "
            "hears from you, and Smart Sending would silently skip decided emails.",
        ]
    else:
        steps += [
            "Add an SMS action whose message is {{ event.body }}. Keep quiet hours on and keep your opt-out "
            "language.",
            "In the SMS settings turn Smart Sending off: Autonomy already decides how often each person hears "
            "from you.",
        ]
    steps.append("Set the message to Live, then set the flow to Live. Autonomy notices within the hour.")
    return tuple(steps)


def _guided(channel: Channel, why: str, *, metric_ready: bool) -> ChannelSetup:
    return ChannelSetup(
        channel=channel,
        ready=False,
        flow_name=FLOW_NAMES[channel],
        detail=why,
        steps=guided_steps(channel, metric_ready=metric_ready),
    )


def _all_guided(account_id: str | None, channels: Sequence[Channel], now: datetime, why: str) -> SetupStatus:
    return SetupStatus(
        account_id=account_id,
        state="guided",
        channels=tuple(_guided(c, why, metric_ready=False) for c in channels),
        detail=why,
        checked_at=now,
    )


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
