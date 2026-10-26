"""One plain sentence for every decision, so a marketer can read the log like a diary.

The sentence is built by code from the numbers the policy already has (no LLM in the inner loop).
It says what happened and the single most useful reason: for a send, what it is expected to add
after costs and why that channel and offer; for "left alone", the rule or the maths that stopped it.

Every rule code in `rules.ALL_CODES` has its own sentence (`BLOCK_SENTENCE`), so a log reader never
meets a bare code. When several rules blocked different options, the one listed first in
`BLOCK_ORDER` is named: rules that stop every message right now (a complaint, a snooze, the weekly
limit) come before rules that stop only some options (an offer, a channel).

**Sends that are not the favourite.** Thompson sampling sends the option with the best *draw*, not
always the one with the best estimate, and the exploration floor now and then picks any option at
random. Those sends are how the agent learns, so their sentence names what is being learned, in the
terms that differ from the favourite: "testing whether WhatsApp beats email for them", "testing
whether 10% off wins them over", "testing whether they buy without free shipping". For a channel
test it adds what we know about this person on that channel ("no WhatsApp messages to them yet").
The point comes first, before any comma, so the Live screen's one-line cut (the words after "Sent
... :" up to the first comma, about 70 characters) shows it. Sentences never carry the customer's
name: the screen shows who it is beside the reason. When no message beat its price, rest was the
favourite, so a send that goes out anyway "tests whether a message beats a rest" (never "more than
the cost"); a rest that restraint enforced is a hold, not a test, and says so.

The favourite is the option with the best estimate after its own channel's attention price (and any
pacing surcharge), the order the auction itself ranks by. Comparing estimates before the price
called a cheap email "a close alternative" to a dearer WhatsApp message it had in fact beaten.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from autonomy.contracts import (
    Bid,
    Channel,
    ChannelStats,
    ContentItem,
    Decision,
    DecisionKind,
    Format,
    Group,
    OfferOption,
    Theme,
)
from autonomy.decide import rules

# How a content theme reads inside "Sent a ... by email".
_THEME_NOUN: dict[Theme, str] = {
    Theme.SAVINGS: "savings message",
    Theme.QUALITY: "quality message",
    Theme.URGENCY: "last-chance message",
    Theme.SOCIAL_PROOF: "social-proof message",
    Theme.EDUCATION: "how-to message",
    Theme.CONVENIENCE: "convenience message",
    Theme.NOVELTY: "what's-new message",
    Theme.LOYALTY: "loyalty message",
    Theme.REMINDER: "reminder",
    Theme.PERSONAL: "personal note",
}

# Why a customer was left alone when every option was blocked, by the rule that blocked it.
BLOCK_SENTENCE: dict[str, str] = {
    rules.HOLDOUT: "Left alone: in the holdout group, which we never contact.",
    rules.NO_BRIEF: "Left alone: no approved brief covers the goals that wanted to act.",
    rules.CHANNEL_NOT_ALLOWED: "Left alone: the brief does not allow any channel that could reach this customer.",
    rules.OFFER_NOT_ALLOWED: "Left alone: the only offers on the table are outside the approved envelope.",
    rules.OFFER_NEEDS_APPROVAL: "Left alone: the only offers worth sending need a person's approval first.",
    rules.NO_DISCOUNTS: "Left alone: this customer asked for no discounts, and every option on the table had one.",
    rules.UNSUBSCRIBED: "Left alone: unsubscribed from every channel we could use.",
    rules.NO_CONSENT: "Left alone: this customer has not agreed to messages on any channel we may use.",
    rules.NO_ADDRESS: "Left alone: we have no address for this customer on any channel we may use.",
    rules.OPEN_COMPLAINT: "Left alone: this customer has an open complaint.",
    rules.SNOOZED: "Left alone: this customer asked us to pause messages for now.",
    rules.TOO_SOON: "Left alone: their last message went out only hours ago or is still waiting to go.",
    rules.WEEKLY_CAP: "Left alone: already reached the weekly contact limit.",
    rules.NO_BUDGET: "Left alone: the goal's budget is used up.",
    rules.QUIET_HOURS: "Left alone: every possible send time falls in quiet hours.",
    rules.PACING: (
        "Left alone: we are slowing down on this channel to stay under the brief's limits on "
        "complaints and unsubscribes."
    ),
    rules.FREQUENCY_PLAN: "Left alone: waiting is worth more for them than a message today, by their 3-week plan.",
}
BLOCK_ORDER: tuple[str, ...] = (
    rules.HOLDOUT,
    rules.OPEN_COMPLAINT,
    rules.SNOOZED,
    rules.WEEKLY_CAP,
    rules.TOO_SOON,
    rules.NO_BUDGET,
    rules.PACING,
    rules.FREQUENCY_PLAN,
    rules.QUIET_HOURS,
    rules.OFFER_NEEDS_APPROVAL,
    rules.NO_DISCOUNTS,
    rules.OFFER_NOT_ALLOWED,
    rules.NO_BRIEF,
)
"""The codes named before the "can't reach them" codes, most binding first. The reach codes
(`rules.REACH_CODES`) come last and are read together: two different reasons on two channels make
one "no channel can reach them" sentence."""
_TIMES = {1: "once", 2: "twice"}


def money(x: float, currency: str = "$") -> str:
    return f"-{currency}{abs(x):,.2f}" if x < 0 else f"{currency}{x:,.2f}"


def _times(n: int) -> str:
    return _TIMES.get(n, f"{n} times")


_CHANNEL_NAME = {
    "email": "email",
    "whatsapp": "WhatsApp",
    "sms": "SMS",
    "push": "push notification",
    "web": "web message",
    "webhook": "the business's own tool",
}


def _channel(channel: str) -> str:
    return _CHANNEL_NAME.get(channel, channel)


def _matching_bid(decision: Decision, bids: Sequence[Bid]) -> Bid | None:
    if decision.action is None:
        return None
    exact = next((b for b in bids if b.action == decision.action), None)
    if exact is not None:
        return exact
    a = decision.action
    return next(
        (
            b
            for b in bids
            if (b.goal_id, b.action.channel, b.action.offer_id, b.action.content_id)
            == (a.goal_id, a.channel, a.offer_id, a.content_id)
        ),
        None,
    )


SHORT_WHY = 70
"""The Live screen shows about this many characters of a reason's point (`shortReason` in live.js)."""
_FOR_THEM = " for them"
_FORMAT_WORDS: dict[Format, str] = {
    Format.TEXT: "plain text",
    Format.TEXT_IMAGE: "an image",
    Format.CAROUSEL: "a carousel",
    Format.VIDEO: "a video",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _offer_words(offer_id: str, offers: Mapping[str, OfferOption]) -> str:
    """How an offer reads inside a sentence: "10% off", "free shipping", "no discount"."""
    if offer_id == "none":
        return "no discount"
    label = offers[offer_id].label if offer_id in offers else offer_id
    return f"{label[:1].lower()}{label[1:]}"


def _theme_words(bid: Bid, content: Mapping[str, ContentItem]) -> str | None:
    """The message's theme as words ("a how-to message", "a reminder"); None if the content is unknown."""
    item = content.get(bid.action.content_id) if bid.action.content_id else None
    if item is None:
        return None
    noun = _THEME_NOUN.get(item.theme, "message")
    return f"{'an' if noun[0] in 'aeiou' else 'a'} {noun}"


def _what_differs(
    chosen: Bid, favourite: Bid, content: Mapping[str, ContentItem], offers: Mapping[str, OfferOption]
) -> tuple[str, str, str] | None:
    """The first way the chosen option differs from the favourite, as (kind, chosen's words, the
    favourite's words); kind is "channel", "offer", "theme" or "format". The order is what a marketer
    reads first: the channel, then the offer, then the message, then its format. None when they
    differ only in ways a sentence cannot name (another goal, a follow-up step)."""
    a, b = chosen.action, favourite.action
    if a.goal_id != b.goal_id:
        return None
    if a.channel is not b.channel:
        return "channel", _channel(a.channel.value), _channel(b.channel.value)
    if a.offer_id != b.offer_id:
        return "offer", _offer_words(a.offer_id, offers), _offer_words(b.offer_id, offers)
    mine, theirs = _theme_words(chosen, content), _theme_words(favourite, content)
    if mine is not None and theirs is not None and mine != theirs:
        return "theme", mine, theirs
    if a.format is not b.format:
        return (
            "format",
            _FORMAT_WORDS.get(a.format, a.format.value),
            _FORMAT_WORDS.get(b.format, b.format.value),
        )
    return None


def _for_them(phrase: str) -> str:
    """Add "for them" when it still fits the screen's one-line cut."""
    return phrase + _FOR_THEM if len(phrase) + len(_FOR_THEM) <= SHORT_WHY else phrase


def _testing(diff: tuple[str, str, str] | None) -> str:
    """What a Thompson-sampled send that is not the favourite is finding out, in plain words."""
    if diff is None:
        return "not enough evidence yet on which message works best for them"
    kind, mine, theirs = diff
    if kind == "offer" and theirs == "no discount":
        return f"testing whether {mine} wins them over"
    if kind == "offer" and mine == "no discount":
        return f"testing whether they buy without {theirs}"
    return _for_them(f"testing whether {mine} beats {theirs}")


def _channel_record(channel: Channel, stats: Mapping[Channel, ChannelStats] | None) -> str | None:
    """What we know about this person on `channel`: "no WhatsApp messages to them yet", "no clicks
    yet from 3 email messages", "2 clicks from 5 WhatsApp messages so far"."""
    if stats is None:
        return None
    st = stats.get(channel)
    name = _channel(channel.value)
    sends = st.sends if st is not None else 0
    clicks = st.clicks if st is not None else 0
    if sends <= 0:
        return f"no {name} messages to them yet"
    if clicks <= 0:
        return f"no clicks yet from {_plural(sends, f'{name} message')}"
    return f"{_plural(clicks, 'click')} from {_plural(sends, f'{name} message')} so far"


def _sent(
    decision: Decision,
    bids: Sequence[Bid],
    content: Mapping[str, ContentItem],
    offers: Mapping[str, OfferOption],
    currency: str,
    explored: bool,
    surcharges: Mapping[tuple[str, Channel], float],
    base_price: float | None,
    prices: Mapping[Channel, float] | None = None,
    channels: Mapping[Channel, ChannelStats] | None = None,
) -> str:
    action = decision.action
    if action is None:  # cannot happen: the contract gives every ACT decision an action
        return "Sent a message."
    item = content.get(action.content_id) if action.content_id else None
    noun = _THEME_NOUN.get(item.theme, "message") if item is not None else "message"
    article = "an" if noun[0] in "aeiou" else "a"
    if action.offer_id == "none":
        offer = "with no discount"
    else:
        label = offers[action.offer_id].label if action.offer_id in offers else action.offer_id
        offer = f"with {label[:1].lower()}{label[1:]}"  # "with free shipping", "with 10% off"
    head = f"Sent {article} {noun} by {_channel(action.channel.value)} {offer}"
    if action.follow_up_step > 0:
        head = f"Sent follow-up {action.follow_up_step}, {article} {noun} by {_channel(action.channel.value)} {offer}"

    chosen = _matching_bid(decision, bids)
    if chosen is None:
        return f"{head}."
    price = decision.attention_price

    own_surcharge = surcharges.get((chosen.goal_id, action.channel), 0.0)
    flat = base_price if base_price is not None else max(price - own_surcharge, 0.0)

    def net(b: Bid) -> float:
        """What the bid is worth once its channel's price and pacing's surcharge are paid. Without
        per-channel prices every bid pays the chosen one's base price."""
        paid = prices.get(b.action.channel, flat) if prices is not None else flat
        return b.expected_net - paid - surcharges.get((b.goal_id, b.action.channel), 0.0)

    best = max(bids, key=net)
    if net(best) <= 0:
        # Doing nothing was the favourite: no message beat its price on its estimate, so this send
        # tests whether messaging this person pays at all ("more than the cost" would be false).
        if explored:
            return f"{head}: a routine test of a message instead of a rest, so every option keeps getting tested."
        record = _channel_record(action.channel, channels)
        numbers = f"{money(net(chosen), currency)} expected after all costs, against {money(0.0, currency)} for a rest"
        tail = f"{record}; {numbers}" if record else numbers
        return f"{head}: testing whether a message beats a rest for them; {tail}."
    if net(best) > net(chosen) + 1e-9:
        diff = _what_differs(chosen, best, content, offers)
        if explored:
            tried = (
                f"a routine test of {diff[1]} instead of {diff[2]}"
                if diff
                else "a routine test of another option"
            )
            return f"{head}: {tried}, so every option keeps getting tested."
        detail = _channel_record(action.channel, channels) if diff and diff[0] == "channel" else None
        mine, theirs = money(net(chosen), currency), money(net(best), currency)
        numbers = (
            f"{mine} expected after all costs, about the same as the current favourite"
            if mine == theirs
            else f"{mine} expected after all costs, against {theirs} for the current favourite"
        )
        tail = f"{detail}; {numbers}" if detail else numbers
        return f"{head}: {_testing(diff)}; {tail}."
    why = f"expected to add {money(chosen.expected_net, currency)} after costs"
    paced = surcharges.get((chosen.goal_id, action.channel), 0.0) > 0
    if paced:
        why += (
            f", more than the {money(price, currency)} a {_channel(action.channel.value)} message costs "
            "while we slow that channel down"
        )
    elif price > 0:
        why += f", more than the {money(price if base_price is None else base_price, currency)} cost of another message"
    extras: list[str] = []
    same_goal = [b for b in bids if b.goal_id == chosen.goal_id]
    if action.offer_id == "none" and any(b.action.offer_id != "none" for b in same_goal):
        extras.append("an offer would not add enough to pay for itself")
    used = {b.action.channel for b in same_goal}
    if len(used) > 1 and chosen.channel_cost <= min(b.channel_cost for b in same_goal):
        extras.append(f"{_channel(action.channel.value)} is cheapest")
    if extras:
        why += "; " + " and ".join(extras)
    return f"{head}: {why}."


def _left_alone(
    decision: Decision,
    bids: Sequence[Bid],
    contacts_7d: int | None,
    currency: str,
    explored: bool,
    surcharges: Mapping[tuple[str, Channel], float],
    prices: Mapping[Channel, float] | None = None,
    restrained: bool = False,
) -> str:
    if decision.group is Group.HOLDOUT:
        return BLOCK_SENTENCE[rules.HOLDOUT]
    price = decision.attention_price
    if bids:

        def cost(b: Bid) -> float:  # its own channel's attention price, before pacing
            return prices.get(b.action.channel, price) if prices is not None else price

        def unpaced(b: Bid) -> float:
            return b.expected_net - cost(b)

        def net(b: Bid) -> float:
            return unpaced(b) - surcharges.get((b.goal_id, b.action.channel), 0.0)

        top = max(bids, key=unpaced)
        if unpaced(top) > 0 and max(net(b) for b in bids) <= 0:
            return (
                f"Left alone: we are slowing down on {_channel(top.action.channel.value)} to stay under "
                "the brief's limits on complaints and unsubscribes, and no other message was worth it."
            )
        best = max(bids, key=lambda b: (net(b), b.expected_net))
        if net(best) <= 0:  # past the check above, no bid beats its price even unpaced
            if cost(top) > 0:
                return (
                    "Left alone: no message is worth more than the cost of contacting "
                    f"(best {money(top.expected_net, currency)} vs {money(cost(top), currency)})."
                )
            return "Left alone: no message is worth more than the cost of contacting."
        if explored:
            return (
                "Left alone this time: resting is still being tested, so we keep measuring what no "
                f"message does (the best message would add about {money(net(best), currency)})."
            )
        if restrained:
            # Restraint kept the option out of the draw: this is a deliberate hold, not a test.
            return (
                "Left alone this time: not sure enough yet that a message pays off for them; the best "
                f"message would add about {money(net(best), currency)} after costs, too uncertain to send."
            )
        return (
            "Left alone this time: testing whether a rest beats a message for them; the best message "
            f"would add about {money(net(best), currency)} after costs."
        )
    blocked = set(decision.blocked_by)
    if not blocked:
        return "Left alone: no active goal has anything to offer this customer."
    for code in BLOCK_ORDER:
        if code not in blocked:
            continue
        if code == rules.WEEKLY_CAP and contacts_7d:
            return f"Left alone: contacted {_times(contacts_7d)} this week, the weekly limit."
        return BLOCK_SENTENCE[code]
    if blocked <= rules.REACH_CODES:
        if len(blocked) == 1:
            return BLOCK_SENTENCE[next(iter(blocked))]
        return "Left alone: no allowed channel can reach this customer (no consent, address or opt-in)."
    known = [c for c in rules.ALL_CODES if c in blocked]
    if known:
        return BLOCK_SENTENCE[known[0]]
    return "Left alone: the hard rules ruled out every option."


def reason(
    decision: Decision,
    bids: Sequence[Bid],
    *,
    content: Mapping[str, ContentItem] | None = None,
    offers: Mapping[str, OfferOption] | None = None,
    contacts_7d: int | None = None,
    explored: bool = False,
    currency: str = "$",
    surcharges: Mapping[tuple[str, Channel], float] | None = None,
    base_price: float | None = None,
    prices: Mapping[Channel, float] | None = None,
    channels: Mapping[Channel, ChannelStats] | None = None,
    restrained: bool = False,
) -> str:
    """One plain sentence for `decision`.

    `bids` are the options that survived the hard rules, with `expected_net` before the attention
    price (the price itself is on the decision). The optional lookups make the sentence richer:
    `content` names the message theme, `offers` the offer label, `contacts_7d` the contact count, and
    `explored` says the choice came from the exploration floor rather than from the sampling.
    `surcharges` are pacing's extra charges per (goal, channel) on top of the attention price, and
    `base_price` the attention price before them (a sent decision's `attention_price` includes its
    channel's surcharge). `prices` are the attention prices per channel (the auction's), so the
    favourite is found the way the auction ranks (None: one price for every bid). `channels` are the
    customer's per-channel records (`CustomerState.channels`), used to say what we know about them on
    the channel a send is testing. `restrained` says restraint kept the best option out of the
    Thompson draw (`AutonomousPolicy` `margin_fn`), so a rest is a deliberate hold, not a test.
    """
    paced = surcharges or {}
    if decision.kind is DecisionKind.ACT:
        return _sent(
            decision,
            bids,
            content or {},
            offers or {},
            currency,
            explored,
            paced,
            base_price,
            prices,
            channels,
        )
    return _left_alone(decision, bids, contacts_7d, currency, explored, paced, prices, restrained)
