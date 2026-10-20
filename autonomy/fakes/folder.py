"""A plain, correct `StateFolder`. WF1 may replace it with a faster or richer one."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from autonomy.contracts import Channel, ChannelStats, CustomerState, Event, EventType

CONTACT_MEMORY = timedelta(days=30)


def _local_hour(ts: datetime, tz: str) -> int:
    try:
        return ts.astimezone(ZoneInfo(tz)).hour
    except (KeyError, ValueError):
        return ts.hour


class SimpleStateFolder:
    def fold(self, state: CustomerState | None, event: Event) -> CustomerState:
        s = state or CustomerState(customer_id=event.customer_id, as_of=event.ts, first_seen_at=event.ts)
        upd: dict[str, object] = {"as_of": max(s.as_of, event.ts), "version": s.version + 1}
        channels: dict[Channel, ChannelStats] = dict(s.channels)
        ch = event.channel
        stats = channels.get(ch, ChannelStats()) if ch else None
        t = event.type

        if t in (EventType.SIGNUP, EventType.VISIT, EventType.VIEW, EventType.CART, EventType.PURCHASE):
            upd["last_seen_at"] = event.ts
        if t is EventType.PURCHASE:
            upd["purchases"] = s.purchases + 1
            upd["revenue"] = s.revenue + (event.value or 0.0)
            upd["margin"] = s.margin + (event.margin or 0.0)
            upd["last_purchase_at"] = event.ts
            if event.category:
                counts = dict(s.category_counts)
                counts[event.category] = counts.get(event.category, 0) + 1
                upd["category_counts"] = counts
                upd["top_categories"] = tuple(sorted(counts, key=lambda c: (-counts[c], c))[:3])
        elif t is EventType.REFUND:
            upd["revenue"] = s.revenue - (event.value or 0.0)
        elif t is EventType.SEND and stats is not None:
            stats = stats.model_copy(update={"sends": stats.sends + 1, "last_send_at": event.ts})
            recent = tuple(x for x in (*s.recent_contacts, event.ts) if x > event.ts - CONTACT_MEMORY)
            upd["recent_contacts"] = recent
            upd["last_contact_at"] = event.ts
        elif t is EventType.DELIVER and stats is not None:
            stats = stats.model_copy(update={"delivers": stats.delivers + 1})
        elif t in (EventType.CLICK, EventType.REPLY) and stats is not None:
            field = "clicks" if t is EventType.CLICK else "replies"
            stats = stats.model_copy(update={field: getattr(stats, field) + 1, "last_engaged_at": event.ts})
            counts24 = list(s.hour_counts or (0,) * 24)
            counts24[_local_hour(event.ts, s.timezone)] += 1
            total = sum(counts24)
            upd["hour_counts"] = tuple(counts24)
            upd["active_hours"] = tuple(c / total for c in counts24)
        elif t is EventType.BOUNCE and stats is not None:
            stats = stats.model_copy(update={"bounces": stats.bounces + 1})
        elif t is EventType.UNSUBSCRIBE and stats is not None:
            stats = stats.model_copy(update={"unsubscribed": True})
        elif t is EventType.COMPLAINT and stats is not None:
            stats = stats.model_copy(update={"complaints": stats.complaints + 1, "unsubscribed": True})
            upd["open_complaint"] = True
        elif t is EventType.CONSENT_GRANTED and stats is not None:
            stats = stats.model_copy(update={"consent": True, "address_known": True, "unsubscribed": False})
        elif t is EventType.CONSENT_REVOKED and stats is not None:
            stats = stats.model_copy(update={"consent": False})
        elif t is EventType.PREFERENCE:
            key, value = event.attrs.get("key"), event.attrs.get("value")
            if isinstance(key, str) and value is not None:
                upd["preferences"] = {**s.preferences, key: str(value)}

        if ch is not None and stats is not None:
            channels[ch] = stats
            upd["channels"] = channels
        return self.refresh(s.model_copy(update=upd), upd["as_of"])  # type: ignore[arg-type]

    def refresh(self, state: CustomerState, now: datetime) -> CustomerState:
        recent = tuple(x for x in state.recent_contacts if x > now - CONTACT_MEMORY)
        week = sum(1 for x in recent if x > now - timedelta(days=7))
        return state.model_copy(
            update={
                "recent_contacts": recent,
                "contacts_7d": week,
                "contacts_30d": len(recent),
                "as_of": max(state.as_of, now),
            }
        )
