"""A past campaign as one row per customer: which message they got (or none), with what probability,
whether they bought in the window after it, and their features before it.

**Which campaign.** Logged sends are SEND events with a channel and `attrs["logged_probability"]` in
(0, 1) (`attrs["probability"]` is read when the former is missing). They are grouped by
`attrs["campaign_id"]` when the history has one, else per channel into bursts (a gap longer than the
learning window starts a new one). The latest campaign whose window has closed (its last send plus the
window is before `now`) with at least `MIN_SENDS` sends is used. Sends without a logged probability
are not used: who got them and why is unknown, so no comparison with them is fair.

**Arms.** A customer's arm is the action key of their first send in the campaign
(`channel|offer|theme[|format]`, from the content library, as the bidders key their options). Arms
with fewer than `MIN_ARM_ROWS` customers are left out, and so are their customers.

**The control group: the convention.** A randomised campaign's control customers got nothing, so the
event history shows nothing for them. Two ways in, in this order:

1. *Explicit*: "do nothing" decisions in the holdout group, made while the campaign ran (how the
   runtime hands a policy a past campaign's assignments). Their `probability` is used.
2. *Inferred*: otherwise every customer the history knew before the campaign started, who got no
   send at all from a learning window before the campaign to a learning window after it, and who was
   reachable on the campaign's channel (consent granted before it started, when the history records
   consent for that channel at all). This assumes the campaign drew from everyone eligible; when it
   did not (a targeted send), the control group does not look like the sent group, and the balance
   check in `autonomy.uplift.model` refuses to fit.

**Propensities.** A treated row's is its send's logged probability. For an inferred control row, when
every arm's logged probabilities are (nearly) constant, the campaign was randomised with a fixed
design, and the chance of getting nothing is one minus the arms' design probabilities. When they vary
(a propensity-logged campaign), a control row's propensity is left unknown here and estimated from
the features in `autonomy.uplift.model`.

**The outcome.** Bought at least once in `[t0, t0 + window)`, where `t0` is the customer's send (or,
for a control customer, the campaign's median send time); `revenue` is what they spent there.

**Features** are the customer's attributes (`CustomerState.preferences`) and behaviour rebuilt from
their events *before* `t0` (`autonomy.uplift.features`), never anything from the window itself.
"""

from __future__ import annotations

import statistics
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import numpy.typing as npt

from autonomy.contracts import (
    NO_OFFER,
    Channel,
    ContentItem,
    CustomerState,
    Decision,
    DecisionKind,
    EventType,
    Group,
)
from autonomy.decide.models import action_key
from autonomy.uplift.features import BEHAVIOUR, EventLike, behaviour_from_events

__all__ = [
    "MIN_ARM_ROWS",
    "MIN_SENDS",
    "CampaignTable",
    "campaign_table",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

MIN_SENDS = 200
"""Fewest logged sends a campaign needs to be learned from."""
MIN_ARM_ROWS = 100
"""Fewest customers an arm needs (the control group too)."""
DESIGN_SPREAD = 0.05
"""An arm's logged probabilities are a fixed design when their range is at most this share of their
median."""
MIN_CONTROL_PROBABILITY = 0.02
"""Below this chance of getting nothing there is no control group to speak of."""


@dataclass(frozen=True, eq=False)
class CampaignTable:
    """One past campaign, one row per customer (see the module docstring)."""

    customer_ids: tuple[str, ...]
    arm: IntArray
    """0 = no message, k = `arm_keys[k - 1]`."""
    arm_keys: tuple[str, ...]
    propensity: FloatArray
    """Chance of the arm the row got; NaN for an inferred control row of a non-randomised campaign."""
    randomised: bool
    y: FloatArray
    """1.0 if they bought in the window, else 0.0."""
    revenue: FloatArray
    attributes: tuple[Mapping[str, str], ...]
    behaviour: FloatArray
    """n x len(BEHAVIOUR), as of each row's t0."""
    channel: Channel
    started_at: datetime
    horizon_days: float
    explicit_control: bool
    notes: tuple[str, ...]

    @property
    def n(self) -> int:
        return len(self.customer_ids)

    @property
    def n_arms(self) -> int:
        return len(self.arm_keys)


@dataclass(frozen=True)
class _Send:
    customer_id: str
    ts: datetime
    channel: Channel
    key: str
    probability: float
    campaign: str


def _probability(e: EventLike) -> float | None:
    for name in ("logged_probability", "probability"):
        raw = e.attrs.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue
        p = float(raw)
        if 0.0 < p < 1.0:
            return p
    return None


def _key(e: EventLike, content_by_id: Mapping[str, ContentItem]) -> str:
    assert e.channel is not None
    raw_content = e.attrs.get("content_id")
    item = content_by_id.get(raw_content) if isinstance(raw_content, str) else None
    raw_offer = e.attrs.get("offer_id")
    offer = raw_offer if isinstance(raw_offer, str) else (item.offer_id if item else NO_OFFER.offer_id)
    if item is not None:
        return action_key(e.channel, offer, item.theme, item.format)
    raw_theme = e.attrs.get("theme")
    raw_format = e.attrs.get("format")
    return action_key(
        e.channel,
        offer,
        raw_theme if isinstance(raw_theme, str) else "*",
        raw_format if isinstance(raw_format, str) else None,
    )


def _campaigns(sends: Sequence[_Send], gap: timedelta) -> dict[str, list[_Send]]:
    """Sends by campaign: their `campaign_id`, else bursts per channel split by a gap > `gap`."""
    out: dict[str, list[_Send]] = {}
    loose: dict[Channel, list[_Send]] = {}
    for s in sends:
        if s.campaign:
            out.setdefault(s.campaign, []).append(s)
        else:
            loose.setdefault(s.channel, []).append(s)
    for channel, items in loose.items():
        items.sort(key=lambda s: (s.ts, s.customer_id))
        start = items[0].ts
        last = items[0].ts
        for s in items:
            if s.ts - last > gap:
                start = s.ts
            last = s.ts
            out.setdefault(f"{channel.value}@{start.isoformat()}", []).append(s)
    return out


def _consented_before(events: Iterable[EventLike], channel: Channel, at: datetime) -> bool:
    granted = False
    for e in sorted(
        (e for e in events if e.channel is channel and e.ts < at), key=lambda e: (e.ts, e.event_id)
    ):
        if e.type is EventType.CONSENT_GRANTED:
            granted = True
        elif e.type in (EventType.CONSENT_REVOKED, EventType.UNSUBSCRIBE):
            granted = False
    return granted


def campaign_table(
    events_by_customer: Mapping[str, Sequence[EventLike]],
    states: Mapping[str, CustomerState],
    content_by_id: Mapping[str, ContentItem],
    now: datetime,
    *,
    horizon_days: float = 7.0,
    decisions: Iterable[Decision] = (),
    exclude: Container[str] = frozenset(),
) -> tuple[CampaignTable | None, list[str]]:
    """The latest closed past campaign in the history as a `CampaignTable`, or None, with plain notes
    on what was used and why anything was refused. Sends whose decision id is in `exclude` (the
    agent's own) are not a past campaign. Pure: time is passed in."""
    notes: list[str] = []
    window = timedelta(days=horizon_days)
    sends: list[_Send] = []
    for cid in sorted(events_by_customer):
        for e in events_by_customer[cid]:
            if e.type is not EventType.SEND or e.channel is None or e.ts > now:
                continue
            if e.decision_id is not None and e.decision_id in exclude:
                continue
            p = _probability(e)
            if p is None:
                continue
            raw_campaign = e.attrs.get("campaign_id")
            sends.append(
                _Send(
                    cid,
                    e.ts,
                    e.channel,
                    _key(e, content_by_id),
                    p,
                    raw_campaign if isinstance(raw_campaign, str) else "",
                )
            )
    if not sends:
        return None, ["no past send logged its probability, so no campaign can be compared fairly"]
    campaigns = _campaigns(sends, window)
    closed = [
        (min(s.ts for s in v), len(v), name, v)
        for name, v in campaigns.items()
        if max(s.ts for s in v) + window <= now and len(v) >= MIN_SENDS
    ]
    if not closed:
        return None, [f"no closed past campaign with at least {MIN_SENDS} logged sends"]
    started, _count, name, chosen = max(closed, key=lambda c: (c[0], c[1], c[2]))
    ended = max(s.ts for s in chosen)
    t_ref = sorted(s.ts for s in chosen)[len(chosen) // 2]
    channel = chosen[0].channel
    notes.append(f"campaign {name}: {len(chosen)} logged sends from {started:%Y-%m-%d} to {ended:%Y-%m-%d}")

    first: dict[str, _Send] = {}
    for s in sorted(chosen, key=lambda s: (s.ts, s.customer_id)):
        first.setdefault(s.customer_id, s)
    by_key: dict[str, list[_Send]] = {}
    for s in first.values():
        by_key.setdefault(s.key, []).append(s)
    design = {k: statistics.median(s.probability for s in v) for k, v in by_key.items()}
    randomised = all(
        (max(s.probability for s in v) - min(s.probability for s in v)) <= DESIGN_SPREAD * design[k]
        for k, v in by_key.items()
    )
    keys = sorted(
        (k for k, v in by_key.items() if len(v) >= MIN_ARM_ROWS), key=lambda k: (-len(by_key[k]), k)
    )
    dropped = sorted(set(by_key) - set(keys))
    if dropped:
        notes.append(f"arms left out for having fewer than {MIN_ARM_ROWS} customers: {', '.join(dropped)}")
    if not keys:
        return None, [*notes, f"no arm of the campaign reached {MIN_ARM_ROWS} customers"]
    arm_of = {k: i + 1 for i, k in enumerate(keys)}

    # Who must not count as control: anyone sent anything around the campaign.
    lo, hi = started - window, ended + window
    touched = {
        cid
        for cid, evs in events_by_customer.items()
        if any(e.type is EventType.SEND and lo <= e.ts <= hi for e in evs)
    }
    explicit: dict[str, float | None] = {}
    for d in decisions:
        if d.kind is DecisionKind.NOTHING and d.group is Group.HOLDOUT and lo <= d.made_at <= hi:
            explicit[d.customer_id] = d.probability if d.probability is not None else None
    p_none = 1.0 - sum(design.values())
    if explicit:
        controls = sorted(c for c in explicit if c not in first)
        notes.append(f"control group: {len(controls)} holdout 'do nothing' decisions of the campaign")
    else:
        records_consent = any(
            e.type is EventType.CONSENT_GRANTED and e.channel is channel
            for evs in events_by_customer.values()
            for e in evs
        )
        known = sorted(set(events_by_customer) | set(states))
        controls = []
        for cid in known:
            if cid in first or cid in touched:
                continue
            evs = events_by_customer.get(cid, ())
            if not any(e.ts < started for e in evs):
                continue
            if records_consent and not _consented_before(evs, channel, started):
                continue
            controls.append(cid)
        notes.append(
            f"control group inferred: {len(controls)} customers known before the campaign, reachable on "
            f"{channel.value} and sent nothing around it (assumes the campaign drew from all of them)"
        )
        if randomised and p_none < MIN_CONTROL_PROBABILITY:
            return None, [*notes, "the campaign's design left almost no one out: no control group"]
    if len(controls) < MIN_ARM_ROWS:
        return None, [*notes, f"control group under {MIN_ARM_ROWS} customers"]

    ids: list[str] = []
    arm: list[int] = []
    prop: list[float] = []
    y: list[float] = []
    revenue: list[float] = []
    attrs: list[Mapping[str, str]] = []
    beh: list[tuple[float, ...]] = []

    def add(cid: str, a: int, p: float, t0: datetime) -> None:
        evs = events_by_customer.get(cid, ())
        bought = [e for e in evs if e.type is EventType.PURCHASE and t0 <= e.ts < t0 + window]
        st = states.get(cid)
        ids.append(cid)
        arm.append(a)
        prop.append(p)
        y.append(1.0 if bought else 0.0)
        revenue.append(float(sum(e.value or 0.0 for e in bought)))
        attrs.append(dict(st.preferences) if st is not None else {})
        beh.append(behaviour_from_events(evs, t0))

    for cid in sorted(first):
        s = first[cid]
        if s.key in arm_of:
            add(cid, arm_of[s.key], s.probability, s.ts)
    for cid in controls:
        if explicit:
            p_exp = explicit.get(cid)
            p = p_exp if p_exp is not None and 0.0 < p_exp < 1.0 else (p_none if randomised else float("nan"))
        else:
            p = p_none if randomised else float("nan")
        add(cid, 0, p, t_ref)
    if not randomised:
        notes.append("logged probabilities vary within an arm: propensity-logged, weighted by 1/p")
    if sum(y) == 0:
        return None, [*notes, "nobody bought in the window: nothing to learn uplift from"]
    table = CampaignTable(
        customer_ids=tuple(ids),
        arm=np.asarray(arm, dtype=np.int64),
        arm_keys=tuple(keys),
        propensity=np.asarray(prop, dtype=np.float64),
        randomised=randomised,
        y=np.asarray(y, dtype=np.float64),
        revenue=np.asarray(revenue, dtype=np.float64),
        attributes=tuple(attrs),
        behaviour=np.asarray(beh, dtype=np.float64).reshape(len(ids), len(BEHAVIOUR)),
        channel=channel,
        started_at=started,
        horizon_days=horizon_days,
        explicit_control=bool(explicit),
        notes=tuple(notes),
    )
    return table, notes
