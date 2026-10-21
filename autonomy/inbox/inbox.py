"""The exceptions-only inbox: open an item while a condition needs a person, close it when it clears.

`Inbox.sync(conditions, now)` is the whole state machine. Every few minutes (a runtime tick hook,
see `autonomy.inbox.install`) the condition builders (`autonomy.inbox.conditions`) say what needs a
person right now, and the inbox reconciles:

* **Open** an item for a cause with no open item, once its condition has lasted `persist_after`
  (a sync delay that heals within the hour never reaches anyone).
* **Keep** an open item while its condition holds, updating its words and money (one item per cause:
  a problem seen by every check, or by two sources, is one item).
* **Resolve** an open item once its condition has been gone for `clear_after` (`resolved_by="cleared"`),
  or when a person presses its button (`resolve(..., by=person)`).
* **No repeats.** An instance of a condition (its `fingerprint`: the alert that started it, the
  status change) that was resolved never reopens. A *new* instance of the same cause within
  `cooldown` (3 days) reopens the same item (counted as a reappearance); after that it is a new
  item (`episode` + 1).

Every open, reopen and resolve is logged (`InboxStore.record`), which is what `appearances` counts:
"items a person had to look at" per week, the founder's 0-2 target on a healthy shop.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from autonomy.contracts import stable_id
from autonomy.inbox.items import Condition, InboxItem, kind_rank
from autonomy.inbox.store import Appearance, InboxStore

__all__ = ["DEFAULT_COOLDOWN", "Inbox", "SyncReport", "WeekCount"]

DEFAULT_COOLDOWN = timedelta(days=3)


@dataclass
class SyncReport:
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    """Causes whose condition holds but has not lasted `persist_after` yet."""


@dataclass(frozen=True)
class WeekCount:
    week_start: datetime
    appearances: int
    causes: tuple[str, ...]


class Inbox:
    """One business's inbox over an `InboxStore`."""

    def __init__(
        self, store: InboxStore, business_id: str, *, cooldown: timedelta = DEFAULT_COOLDOWN
    ) -> None:
        self.store = store
        self.business_id = business_id
        self.cooldown = cooldown

    # --- reading -----------------------------------------------------------------------------

    def open_items(self) -> list[InboxItem]:
        """The items waiting for a person, harm first."""
        items = self.store.items(self.business_id, open_only=True)
        return sorted(items, key=lambda i: (kind_rank(i.kind), -i.opened_at.timestamp(), i.item_id))

    def handled_at(self, cause: str) -> datetime | None:
        """When a person last resolved this cause (the replies builder counts only newer replies)."""
        item = self.store.latest(self.business_id, cause)
        if item is None or item.resolved_by != "person":
            return None
        return item.resolved_at

    def appearances(self, since: datetime, until: datetime) -> list[Appearance]:
        """Items a person had to look at in `[since, until)`: every open and reopen."""
        return [
            a
            for a in self.store.appearances(self.business_id, since=since, until=until)
            if a.what in ("opened", "reopened")
        ]

    def weekly_counts(self, start: datetime, end: datetime) -> list[WeekCount]:
        """Appearances per 7-day week from `start` (the last week may be short)."""
        out: list[WeekCount] = []
        week = start
        while week < end:
            stop = min(week + timedelta(days=7), end)
            seen = self.appearances(week, stop)
            out.append(WeekCount(week, len(seen), tuple(a.cause for a in seen)))
            week = stop
        return out

    # --- the state machine -------------------------------------------------------------------

    def sync(
        self, conditions: Iterable[Condition], now: datetime, *, cleared: Iterable[str] = ()
    ) -> SyncReport:
        """Reconcile with the current conditions. `cleared` names causes that are decisively gone:
        their items resolve now, whatever their `clear_after`."""
        report = SyncReport()
        current = _merge(conditions)
        gone_now = set(cleared) - set(current)
        for _, cond in sorted(current.items()):
            self._apply(cond, now, report)
        for item in self.store.items(self.business_id, open_only=True):
            if item.cause in current:
                continue  # still there: `_apply` refreshed it
            gone_since = item.missing_since or now
            if item.cause in gone_now or now - gone_since >= timedelta(seconds=item.clear_after_s):
                self._resolve(item, now, by="cleared", report=report)
            elif item.missing_since is None:
                self.store.put(item.model_copy(update={"missing_since": now}))
        return report

    def resolve(self, item_id: str, now: datetime, *, by: str) -> InboxItem | None:
        """A person pressed the item's button (or resolved it another way). Returns the item, or None."""
        item = self.store.get(item_id)
        if item is None or item.business_id != self.business_id:
            return None
        if not item.open:
            return item
        return self._resolve(item, now, by="person", who=by)

    # --- internals ---------------------------------------------------------------------------

    def _apply(self, cond: Condition, now: datetime, report: SyncReport) -> None:
        latest = self.store.latest(self.business_id, cond.cause)
        if latest is not None and latest.open:
            updated = latest.model_copy(
                update={
                    "sentence": cond.sentence,
                    "action": cond.action,
                    "button": cond.button,
                    "detail": cond.detail,
                    "money": cond.money,
                    "currency": cond.currency,
                    "fingerprint": cond.fingerprint,
                    "last_seen_at": now,
                    "missing_since": None,
                    "clear_after_s": cond.clear_after.total_seconds(),
                    "refs": tuple(dict.fromkeys((*latest.refs, *cond.refs)))[-20:],
                    "goal_id": cond.goal_id,
                }
            )
            if updated != latest:
                self.store.put(updated)
                report.updated.append(latest.item_id)
            return
        if now < cond.since + cond.persist_after:
            report.waiting.append(cond.cause)
            return
        if latest is not None and cond.fingerprint in (latest.fingerprint, latest.handled_fingerprint):
            return  # this instance was already resolved (by a person, or it cleared): never again
        if (
            latest is not None
            and latest.resolved_by == "cleared"
            and latest.resolved_at is not None
            and now - latest.resolved_at < self.cooldown
        ):
            item = self._fresh(cond, now, episode=latest.episode).model_copy(
                update={
                    "item_id": latest.item_id,
                    "opened_at": latest.opened_at,
                    "reopened": latest.reopened + 1,
                }
            )
            self.store.put(item)
            self._log(item, now, "reopened")
            report.reopened.append(item.item_id)
            return
        episode = 1 if latest is None else latest.episode + 1
        item = self._fresh(cond, now, episode=episode)
        self.store.put(item)
        self._log(item, now, "opened")
        report.opened.append(item.item_id)

    def _fresh(self, cond: Condition, now: datetime, *, episode: int) -> InboxItem:
        return InboxItem(
            item_id=stable_id("inbox", self.business_id, cond.cause, episode),
            business_id=self.business_id,
            cause=cond.cause,
            kind=cond.kind,
            goal_id=cond.goal_id,
            sentence=cond.sentence,
            action=cond.action,
            button=cond.button,
            detail=cond.detail,
            money=cond.money,
            currency=cond.currency,
            opened_at=now,
            last_seen_at=now,
            episode=episode,
            fingerprint=cond.fingerprint,
            clear_after_s=cond.clear_after.total_seconds(),
            refs=cond.refs,
        )

    def _resolve(
        self, item: InboxItem, now: datetime, *, by: str, report: SyncReport | None = None, who: str = ""
    ) -> InboxItem:
        person = by == "person"
        done = item.model_copy(
            update={
                "resolved_at": now,
                "resolved_by": "person" if person else "cleared",
                "handled_fingerprint": item.fingerprint if person else item.handled_fingerprint,
                "missing_since": None,
            }
        )
        self.store.put(done)
        self._log(done, now, "resolved", by=who or ("person" if person else "cleared"))
        if report is not None:
            report.resolved.append(item.item_id)
        return done

    def _log(self, item: InboxItem, now: datetime, what: str, *, by: str = "") -> None:
        self.store.record(
            Appearance(
                item_id=item.item_id,
                business_id=self.business_id,
                cause=item.cause,
                kind=item.kind.value,
                at=now,
                what="opened" if what == "opened" else "reopened" if what == "reopened" else "resolved",
                by=by,
            )
        )


def _merge(conditions: Iterable[Condition]) -> dict[str, Condition]:
    """One condition per cause: two sources for the same cause (the breaker's alert and Autopilot's
    step back to Approve) are one item; the most recent instance names it, the earliest start counts."""
    out: dict[str, Condition] = {}
    for c in conditions:
        seen = out.get(c.cause)
        if seen is None:
            out[c.cause] = c
            continue
        newest, other = (c, seen) if c.since >= seen.since else (seen, c)
        out[c.cause] = Condition(
            cause=c.cause,
            kind=newest.kind,
            sentence=newest.sentence,
            action=newest.action,
            button=newest.button,
            fingerprint=newest.fingerprint,
            since=min(c.since, seen.since),
            goal_id=newest.goal_id,
            detail=newest.detail,
            money=newest.money if newest.money is not None else other.money,
            currency=newest.currency or other.currency,
            persist_after=min(c.persist_after, seen.persist_after),
            clear_after=max(c.clear_after, seen.clear_after),
            refs=tuple(dict.fromkeys((*seen.refs, *c.refs))),
        )
    return out
