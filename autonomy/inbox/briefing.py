"""Everything that is not an inbox item goes to the weekly briefing, counted, in one quiet section.

`quiet_week(alerts, appearances, since, until)` turns the week's alerts that `route_alert` sent to
the briefing (and the inbox items that came and went) into one `BriefingSection` for the weekly
briefing: "Handled without you". Every number in its words is in its `numbers`, as the briefing's
claim checker requires; faults routed to the operator (`OPS`) are counted but never worded for the
shop owner.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime

from autonomy.budget.models import MAX_WEEKLY_SHARE, BudgetMove
from autonomy.budget.report import move_entry
from autonomy.contracts import Alert, BriefingSection
from autonomy.inbox.items import BRIEFING, OPS, route_alert
from autonomy.inbox.store import Appearance

__all__ = ["BUDGET_TITLE", "TOPIC_WORDS", "budget_week", "quiet_week"]

BUDGET_TITLE = "Budget moves"

TOPIC_WORDS: dict[str, tuple[str, str]] = {
    "data": ("data note", "data notes"),
    "connections": ("connection note", "connection notes"),
    "limits": ("time the limits paced spend", "times the limits paced spend"),
    "approvals": ("batch that expired unapproved", "batches that expired unapproved"),
    "plan": ("weekly plan review left unchanged", "weekly plan reviews left unchanged"),
    "sending": ("sending note", "sending notes"),
    "customers": ("customer note", "customer notes"),
    "other": ("other note", "other notes"),
}


def quiet_week(
    alerts: Iterable[Alert], appearances: Iterable[Appearance], since: datetime, until: datetime
) -> BriefingSection:
    """The week's quiet lines: what was noted, and what needed a person and cleared."""
    topics: Counter[str] = Counter()
    ops = 0
    for a in alerts:
        if not since <= a.at < until:
            continue
        route = route_alert(a)
        if route.destination == BRIEFING:
            topics[route.topic] += 1
        elif route.destination == OPS:
            ops += 1
    lines = [a for a in appearances if since <= a.at < until]
    opened = sum(1 for a in lines if a.what in ("opened", "reopened"))
    cleared = sum(1 for a in lines if a.what == "resolved" and a.by == "cleared")
    numbers: dict[str, float] = {"inbox_items": float(opened), "cleared_on_their_own": float(cleared)}
    parts: list[str] = []
    for topic, n in sorted(topics.items(), key=lambda kv: (-kv[1], kv[0])):
        one, many = TOPIC_WORDS.get(topic, TOPIC_WORDS["other"])
        parts.append(f"{n} {one if n == 1 else many}")
        numbers[f"notes_{topic}"] = float(n)
    numbers["faults_for_operator"] = float(ops)
    if opened == 0:
        head = "Nothing needed you this week."
    else:
        head = f"{opened} {'thing' if opened == 1 else 'things'} needed you this week"
        head += f"; {cleared} cleared on {'its' if cleared == 1 else 'their'} own." if cleared else "."
    body = head if not parts else f"{head} Noted, nothing to do: {', '.join(parts)}."
    return BriefingSection(title="Handled without you", body=body, numbers=numbers)


def budget_week(
    moves: Iterable[BudgetMove],
    since: datetime,
    until: datetime,
    names: Mapping[str, str],
    currency: str = "USD",
    *,
    cap: float = MAX_WEEKLY_SHARE,
) -> BriefingSection | None:
    """The week's auto-budget moves, one plain line each with its numbers, or None when none were made.

    `cap` is the weekly limit in force (`BudgetConfig.max_weekly_share`), so the line states it.

    Only moves still applied are listed; one a person undid is no longer a change to report. None keeps
    the weekly briefing exactly as it was while auto-budget is off.
    """
    chosen = [m for m in moves if m.status == "applied" and since <= m.at < until]
    if not chosen:
        return None
    lines: list[str] = []
    numbers: dict[str, float] = {}
    for i, move in enumerate(sorted(chosen, key=lambda m: (m.at, m.move_id))):
        text, nums = move_entry(move, names, currency, cap=cap)
        lines.append(text)
        numbers.update({k if i == 0 else f"{k}_{i}": v for k, v in nums.items()})
    return BriefingSection(title=BUDGET_TITLE, body=" ".join(lines), numbers=numbers)
