"""Conversation: what to do when a customer replies (plan-v2 section 2, minimal in v2.0).

A reply is tagged with one intent: stop, not_now, question, complaint, interested or other. The
intent becomes events the store records (so customer state, and therefore `decide()`, reacts) and,
where a person should look, a hand-off flag.

Safety first, in this order:

1. **A hard keyword net.** STOP, UNSUBSCRIBE, "opt out", "remove me" and the like always mean stop,
   whatever a classifier says. Stop is honoured at once: an UNSUBSCRIBE event for the reply's channel
   (and, by default, every other channel we know for the customer), stamped with the reply's time so
   anything sent after the customer asked shows up as a breach. A reply that says "stop" in passing
   ("can't stop wearing these") is also treated as stop: a missed opt-out is a legal problem, a
   wrong one costs one customer's messages.
   On WhatsApp replies, opt-outs in Hindi, Hinglish and other languages come from
   `configs/v4/optout_phrases.yaml` (`autonomy.channels.optout`): add a language there, not in code.
   Email and SMS replies use the English net only, as before. A WhatsApp reply the adapter already
   flagged (`opted_out` on its attrs) is stop, and the adapter has written that channel's
   UNSUBSCRIBE: only the other channels' are added here, so each channel gets one.
2. **Complaint words** (spam, scam, harassment, "report you") always mean complaint, unless stop won.
3. Only then the swappable **text classifier** (`autonomy.interfaces.TextSignals`). A low-confidence
   answer or a classifier failure is handed to a person.

No LLM writes to customers in v2.0: `suggested_reply` is one of a few fixed, reviewed sentences.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Literal, cast, get_args

from pydantic import Field

from autonomy.agents.base import load_prompt
from autonomy.channels.optout import OPTED_OUT_ATTR, opt_out_phrase
from autonomy.contracts import Channel, Contract, CustomerState, Event, EventType, stable_id
from autonomy.interfaces import TextSignals

Intent = Literal["stop", "not_now", "question", "complaint", "interested", "other"]
INTENTS: tuple[str, ...] = get_args(Intent)

MIN_CONFIDENCE = 0.6
"""Below this the classifier's answer is still used, but a person is asked to check it."""

DEFAULT_SNOOZE_DAYS = 30
SOURCE = "conversation"

STOP_EXACT: frozenset[str] = frozenset(
    {
        "stop",
        "stopall",
        "stop all",
        "unsubscribe",
        "unsub",
        "cancel",
        "end",
        "quit",
        "optout",
        "opt out",
        "remove",
        "arret",
        "arrêt",
        "baja",
        "parar",
        "alto",
        "stopp",
    }
)
"""Whole-message opt-out keywords, including the carrier-standard ones (CANCEL, END, QUIT) and a few
common non-English ones. Matched after lower-casing and dropping punctuation."""

STOP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\bstop\b",
        r"\bunsub(?:scrib\w*)?\b",
        r"\bopt(?:ed|ing)?[\s\-]?out\b",
        r"\bremove me\b",
        r"\b(?:remove|delete) my (?:number|email|e-mail|address|details|data|contact)\b",
        r"\btake me off\b",
        r"\b(?:don't|dont|do not|never) (?:contact|message|text|email|e-mail|call|write to|send) me\b",
        r"\b(?:don't|dont|do not) (?:send|message|text|email) (?:me )?any ?more\b",
        r"\bno more (?:messages|texts|emails|e-mails|marketing)\b",
        r"\bleave me alone\b",
    )
)
"""Opt-out phrases matched anywhere in the reply."""

COMPLAINT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\bspam(?:ming|med|my)?\b",
        r"\bscam\w*\b",
        r"\bharass\w*\b",
        r"\breport(?:ed|ing)? (?:you|this|them)\b",
    )
)

SNOOZE_HINTS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\btomorrow\b"), 1),
    (re.compile(r"\bnext year\b|\b(?:few|couple of|two|three) months\b"), 90),
    (re.compile(r"\b(?:next|a|one) month\b|\b(?:few|couple of|two|three) weeks\b"), 30),
    (re.compile(r"\b(?:next|a|one|this) week\b|\b(?:few|couple of) days\b|\bweekend\b"), 7),
)
"""How long "not now" lasts when the reply says; otherwise `DEFAULT_SNOOZE_DAYS`."""

REPLY_TEMPLATES: dict[str, str | None] = {
    "stop": "Done. You won't get any more messages from us.",
    "not_now": "No problem. We'll give you some space.",
    "question": "Thanks for your message. Someone from our team will reply soon.",
    "complaint": "We're sorry about this. Someone from our team will be in touch soon.",
    "interested": "Great, thank you. Someone from our team will follow up shortly.",
    "other": None,
}
"""The only words v2.0 suggests sending to a customer. Fixed and reviewed; no model writes them."""

NEEDS_HUMAN: dict[str, bool] = {
    "stop": False,
    "not_now": False,
    "question": True,
    "complaint": True,
    "interested": True,
    "other": False,
}


class ReplyOutcome(Contract):
    intent: Intent
    confidence: float = Field(ge=0, le=1)
    events_to_record: list[Event] = Field(default_factory=list)
    needs_human: bool
    suggested_reply: str | None = None
    reason: str = Field(description="Plain English: why this intent")
    matched: str | None = Field(default=None, description="The keyword or phrase that forced the intent")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'").replace("‘", "'").lower()).strip()


def stop_keyword(text: str, channel: Channel | None = None) -> str | None:
    """The opt-out word or phrase in `text`, if any. The hard safety net: no classifier overrides it.

    A WhatsApp reply is also checked against the multilingual list (`autonomy.channels.optout`)."""
    norm = _normalise(text)
    bare = re.sub(r"[^\w\s\-]", "", norm).strip()
    if bare in STOP_EXACT or bare.replace("-", " ") in STOP_EXACT:
        return bare
    for pattern in STOP_PATTERNS:
        found = pattern.search(norm)
        if found:
            return found.group(0)
    if channel is Channel.WHATSAPP:
        return opt_out_phrase(text)  # Hindi, Hinglish, ...: configs/v4/optout_phrases.yaml
    return None


def complaint_keyword(text: str) -> str | None:
    norm = _normalise(text)
    for pattern in COMPLAINT_PATTERNS:
        found = pattern.search(norm)
        if found:
            return found.group(0)
    return None


def snooze_days(text: str) -> int:
    norm = _normalise(text)
    return next((days for pattern, days in SNOOZE_HINTS if pattern.search(norm)), DEFAULT_SNOOZE_DAYS)


class Conversation:
    def __init__(self, signals: TextSignals, *, stop_all_channels: bool = True) -> None:
        """`stop_all_channels`: a stop on one channel unsubscribes every channel we know for the
        customer. On by default: someone who replies STOP rarely wants to hear from us elsewhere."""
        self.signals = signals
        self.stop_all_channels = stop_all_channels

    def classify(self, text: str, channel: Channel | None = None) -> tuple[Intent, float, str | None, str]:
        """(intent, confidence, matched keyword, reason). Keyword nets first, then the classifier.

        `channel` is the reply's channel: WhatsApp replies also go through the multilingual list."""
        word = stop_keyword(text, channel)
        if word is not None:
            return "stop", 1.0, word, f"The reply contains the opt-out word '{word}'."
        word = complaint_keyword(text)
        if word is not None:
            return "complaint", 1.0, word, f"The reply contains the complaint word '{word}'."
        if not text.strip():
            return "other", 1.0, None, "The reply has no text."
        try:
            label, prob = self.signals.classify([text], question=load_prompt("conversation"), labels=INTENTS)[
                0
            ]
        except Exception as err:  # any classifier failure goes to a person, never to a guess
            return (
                "other",
                0.0,
                None,
                f"The classifier failed ({type(err).__name__}); a person should read it.",
            )
        if label not in INTENTS:
            return "other", 0.0, None, f"The classifier answered an unknown label '{label}'."
        try:
            confidence = float(prob)
        except (TypeError, ValueError):
            confidence = math.nan
        if not math.isfinite(confidence):  # a NaN would fail ReplyOutcome; treat it as "not sure"
            confidence = 0.0
        confidence = min(max(confidence, 0.0), 1.0)
        return cast(Intent, label), confidence, None, f"The classifier read it as '{label}'."

    def handle_reply(self, event: Event, state: CustomerState | None, now: datetime) -> ReplyOutcome:
        """What a reply means and the events to record. Stop is honoured in the events returned here."""
        if event.type is not EventType.REPLY:
            raise ValueError(f"handle_reply takes a reply event, not {event.type.value}")
        if state is not None and state.customer_id != event.customer_id:
            raise ValueError("the state belongs to a different customer than the reply")
        channel = event.channel
        if channel is None:  # a reply event always has one (Event validates it); this keeps mypy honest
            raise ValueError("a reply event needs a channel")

        text = event.text or ""
        raw_flag = event.attrs.get(OPTED_OUT_ATTR)
        flagged = raw_flag if isinstance(raw_flag, str) and raw_flag else None
        intent, confidence, matched, reason = (
            ("stop", 1.0, flagged, f"The reply is the opt-out phrase '{flagged}'.")
            if flagged is not None
            else self.classify(text, channel)
        )
        events: list[Event] = []

        def derived(kind: EventType, ch: Channel | None, **attrs: str) -> Event:
            return Event(
                event_id=stable_id("ev", SOURCE, event.event_id, kind.value, ch.value if ch else ""),
                customer_id=event.customer_id,
                type=kind,
                ts=event.ts,
                source=SOURCE,
                channel=ch,
                decision_id=event.decision_id if ch is channel else None,
                attrs={"reply_event_id": event.event_id, **attrs},
            )

        if intent == "stop":
            channels = {channel}
            if self.stop_all_channels and state is not None:
                channels |= set(state.channels)
            if flagged is not None:
                channels.discard(channel)  # the adapter wrote this channel's UNSUBSCRIBE already
            for ch in sorted(channels, key=lambda c: (c is not channel, c.value)):
                events.append(derived(EventType.UNSUBSCRIBE, ch, reason="reply_stop"))
        elif intent == "not_now":
            until = max(event.ts, now) + timedelta(days=snooze_days(text))
            events.append(derived(EventType.PREFERENCE, channel, key="snooze_until", value=until.isoformat()))
        elif intent == "complaint":
            events.append(derived(EventType.COMPLAINT, channel, reason="reply_complaint"))

        needs_human = NEEDS_HUMAN[intent]
        if intent != "stop" and text.strip() and confidence < MIN_CONFIDENCE:
            needs_human = True
            reason += " The classifier was not sure, so a person should check."
        if intent != "stop" and state is not None and state.open_complaint:
            needs_human = True
            reason += " This customer has an open complaint."
        return ReplyOutcome(
            intent=intent,
            confidence=confidence,
            events_to_record=events,
            needs_human=needs_human,
            suggested_reply=REPLY_TEMPLATES[intent],
            reason=reason,
            matched=matched,
        )
