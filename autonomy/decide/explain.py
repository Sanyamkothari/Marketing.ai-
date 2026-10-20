"""One plain sentence for every decision, so a marketer can read the log like a diary.

The sentence is built by code from the numbers the policy already has (no LLM in the inner loop).
It says what happened and the single most useful reason: for a send, what it is expected to add
after costs and why that channel and offer; for "left alone", the rule or the maths that stopped it.

Every rule code in `rules.ALL_CODES` has its own sentence (`BLOCK_SENTENCE`), so a log reader never
meets a bare code. When several rules blocked different options, the one listed first in
`BLOCK_ORDER` is named: rules that stop every message right now (a complaint, a snooze, the weekly
limit) come before rules that stop only some options (an offer, a channel).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from autonomy.contracts import Bid, Channel, ContentItem, Decision, DecisionKind, Group, OfferOption, Theme
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
}
BLOCK_ORDER: tuple[str, ...] = (
    rules.HOLDOUT,
    rules.OPEN_COMPLAINT,
    rules.SNOOZED,
    rules.WEEKLY_CAP,
    rules.TOO_SOON,
    rules.NO_BUDGET,
    rules.PACING,
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


def _sent(
    decision: Decision,
    bids: Sequence[Bid],
    content: Mapping[str, ContentItem],
    offers: Mapping[str, OfferOption],
    currency: str,
    explored: bool,
    surcharges: Mapping[tuple[str, Channel], float],
    base_price: float | None,
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

    def net(b: Bid) -> float:  # what the bid is worth once pacing's surcharge on its channel is paid
        return b.expected_net - surcharges.get((b.goal_id, b.action.channel), 0.0)

    best = max(bids, key=net)
    if net(best) > net(chosen):
        if explored:
            return f"{head}: trying a less-favoured option so every option keeps getting tested."
        return f"{head}: the top option's estimate is uncertain, so a close alternative won this draw."
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
    channels = {b.action.channel for b in same_goal}
    if len(channels) > 1 and chosen.channel_cost <= min(b.channel_cost for b in same_goal):
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
) -> str:
    if decision.group is Group.HOLDOUT:
        return BLOCK_SENTENCE[rules.HOLDOUT]
    price = decision.attention_price
    if bids:
        best = max(bids, key=lambda b: b.expected_net)

        def net(b: Bid) -> float:
            return b.expected_net - surcharges.get((b.goal_id, b.action.channel), 0.0)

        if best.expected_net - price > 0 and max(net(b) for b in bids) - price <= 0:
            return (
                f"Left alone: we are slowing down on {_channel(best.action.channel.value)} to stay under "
                "the brief's limits on complaints and unsubscribes, and no other message was worth it."
            )
        best = max(bids, key=lambda b: (net(b), b.expected_net))
        if net(best) - price <= 0:  # past the check above, no bid beats the price even unpaced
            if price > 0:
                top = max(b.expected_net for b in bids)
                return (
                    "Left alone: no message is worth more than the cost of contacting "
                    f"(best {money(top, currency)} vs {money(price, currency)})."
                )
            return "Left alone: no message is worth more than the cost of contacting."
        if explored:
            return (
                "Left alone this time: resting is still being tested, so we keep measuring what no "
                f"message does (the best message would add about {money(net(best) - price, currency)})."
            )
        return (
            "Left alone this time: the best message "
            f"(about {money(net(best) - price, currency)} after costs) is uncertain, "
            "so resting is still being tested."
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
) -> str:
    """One plain sentence for `decision`.

    `bids` are the options that survived the hard rules, with `expected_net` before the attention
    price (the price itself is on the decision). The optional lookups make the sentence richer:
    `content` names the message theme, `offers` the offer label, `contacts_7d` the contact count, and
    `explored` says the choice came from the exploration floor rather than from the sampling.
    `surcharges` are pacing's extra charges per (goal, channel) on top of the attention price, and
    `base_price` the attention price before them (a sent decision's `attention_price` includes its
    channel's surcharge).
    """
    paced = surcharges or {}
    if decision.kind is DecisionKind.ACT:
        return _sent(decision, bids, content or {}, offers or {}, currency, explored, paced, base_price)
    return _left_alone(decision, bids, contacts_7d, currency, explored, paced)
