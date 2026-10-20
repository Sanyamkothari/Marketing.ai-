"""Klaviyo: read profiles, consent and engagement; send Autonomy's decisions through the shop's own flows.

* `connector` - `KlaviyoConnector` (the registry loads it as `autonomy.connectors.klaviyo:KlaviyoConnector`);
* `api` - the pinned revision, OAuth provider, scopes, per-endpoint rate limits and `EndpointPacer`;
* `client` - JSON:API calls with per-endpoint pacing and Klaviyo's error details;
* `mapping` - profiles and metric events as Autonomy events;
* `setup` - the send flows and template created by API, or the exact guided steps where it cannot.

The send adapter that the dispatcher and gateway use is `autonomy.channels.klaviyo`.
"""

from autonomy.connectors.klaviyo.api import (
    API_REVISION,
    DECISION_METRIC,
    ENDPOINT_LIMITS,
    KLAVIYO_OAUTH,
    KLAVIYO_SCOPES,
    EndpointLimit,
    EndpointPacer,
)
from autonomy.connectors.klaviyo.connector import (
    DECISION_EVENT,
    Audience,
    KlaviyoConnector,
    channels_for,
    decision_event_body,
)
from autonomy.connectors.klaviyo.mapping import (
    BEHAVIOUR_METRICS,
    ENGAGEMENT_METRICS,
    HandoffLookup,
    customer_for,
)
from autonomy.connectors.klaviyo.setup import (
    EMAIL_FLOW,
    EMAIL_TEMPLATE,
    SMS_FLOW,
    ChannelSetup,
    SetupStatus,
    ensure_send_setup,
    guided_steps,
)

__all__ = [
    "API_REVISION",
    "BEHAVIOUR_METRICS",
    "DECISION_EVENT",
    "DECISION_METRIC",
    "EMAIL_FLOW",
    "EMAIL_TEMPLATE",
    "ENDPOINT_LIMITS",
    "ENGAGEMENT_METRICS",
    "KLAVIYO_OAUTH",
    "KLAVIYO_SCOPES",
    "SMS_FLOW",
    "Audience",
    "ChannelSetup",
    "EndpointLimit",
    "EndpointPacer",
    "HandoffLookup",
    "KlaviyoConnector",
    "SetupStatus",
    "channels_for",
    "customer_for",
    "decision_event_body",
    "ensure_send_setup",
    "guided_steps",
]
