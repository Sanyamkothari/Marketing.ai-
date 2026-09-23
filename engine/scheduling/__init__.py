"""Scheduling, monitoring and outcomes (Phase 4b M49).

The modules, in the order a reader meets them: `cron` (the five-field parser and its EventBridge
translation), `schedules` (the `Schedule`, its firings, the two tables and the due-slot rule),
`alerts` (the alert record, its table and where alerts are sent), `scheduler` (what makes a schedule
fire: nothing, an in-process thread, or EventBridge Scheduler), `firing` (what a firing *does*: score,
check drift, retrain - through the same engine functions the API's own routes call), `retraining`
(`monitoring.retraining` made live, and the erasure retraining flags), `service` (the operations the
API exposes) and `outcomes` (real-world performance once outcome windows mature, and the input Plan
B's incrementality report reads).

Every firing acts as `engine.access.roles.SYSTEM_SCHEDULER`, which holds the Analyst role only: a
schedule can score, check drift and train a challenger, and can never approve one.
"""
