"""Zero-touch: an exceptions-only inbox, one-click Autopilot and a quiet data watchdog.

After a one-time connect, a healthy shop should need a person for almost nothing: 0-2 inbox items a
week (docs/v2/ZERO_TOUCH.md). This package decides what those are.

* `items`: the seven kinds of item a person must act on, and `route_alert`: where every alert code
  goes (inbox, weekly briefing, or the operator's log).
* `conditions`: one builder per kind, reading the system's state (a paused goal, a connection that
  needs reconnecting, a goal that earned Autopilot), so an item clears by itself.
* `inbox`: the state machine (one item per cause, resolve on clear, no repeats, a 3-day cool-down)
  and the weekly count.
* `watchdog`: missing days, row-count drops, schema changes, consent coverage and duplicate customers
  against the shop's own normal; its holds feed `HealthMonitor.ready_to_act`.
* `briefing`: the weekly "Handled without you" section for everything that is not an item.
* `install`: `ZeroTouch` (tick hook and the one button) and `install(runtime, ...)`.

Autopilot's bar, the one-click promotion, automatic demotion and the autonomy ledger live in
`autonomy.runtime.promotion`.
"""

from autonomy.inbox.briefing import quiet_week
from autonomy.inbox.conditions import Collected, InboxSources, collect
from autonomy.inbox.inbox import Inbox, SyncReport, WeekCount
from autonomy.inbox.items import ROUTES, Button, Condition, InboxItem, InboxKind, Route, route_alert
from autonomy.inbox.store import (
    Appearance,
    InboxStore,
    InMemoryInboxStore,
    SqlInboxStore,
    create_inbox_tables,
)
from autonomy.inbox.watchdog import (
    DataWatchdog,
    Finding,
    InMemoryWatchdogStore,
    SqlWatchdogStore,
    WatchdogPolicy,
)

__all__ = [
    "ROUTES",
    "Appearance",
    "Button",
    "Collected",
    "Condition",
    "DataWatchdog",
    "Finding",
    "InMemoryInboxStore",
    "InMemoryWatchdogStore",
    "Inbox",
    "InboxItem",
    "InboxKind",
    "InboxSources",
    "InboxStore",
    "Route",
    "SqlInboxStore",
    "SqlWatchdogStore",
    "SyncReport",
    "WatchdogPolicy",
    "WeekCount",
    "collect",
    "create_inbox_tables",
    "quiet_week",
    "route_alert",
]
