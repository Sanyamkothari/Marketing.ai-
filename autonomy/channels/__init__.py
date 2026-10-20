"""Channels (WF5): sending decisions out and keeping customers safe while we do.

- `render`: fills a content item's `{{slots}}` for one customer.
- `dispatcher`: turns ACT decisions into messages through channel adapters, exactly once each.
- `common`: `send_event_id`, the one SEND event id the dispatcher and every adapter use.
- `breakers`: circuit breakers that pause goals or channels when complaints, unsubscribes,
  failed deliveries, overspend or a poor WhatsApp rating show harm.

Provider adapters (SES, WhatsApp Cloud API, webhook/CSV) live in their own modules here and are
imported directly, so importing this package stays light.
"""

from autonomy.channels.breakers import BreakerResult, Guards, evaluate, evaluate_goals
from autonomy.channels.common import send_event_id
from autonomy.channels.dispatcher import (
    SEND_JOB_KIND,
    Dispatcher,
    DispatchReport,
    DispatchResult,
    DispatchStatus,
    SkipReason,
)
from autonomy.channels.render import KNOWN_SLOTS, Rendered, RenderError, render

__all__ = [
    "KNOWN_SLOTS",
    "SEND_JOB_KIND",
    "BreakerResult",
    "DispatchReport",
    "DispatchResult",
    "DispatchStatus",
    "Dispatcher",
    "Guards",
    "RenderError",
    "Rendered",
    "SkipReason",
    "evaluate",
    "evaluate_goals",
    "render",
    "send_event_id",
]
