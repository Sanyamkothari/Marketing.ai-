"""The who book through its own wiring: `ingest`, `refresh` (the trait groups keyed from the profile of
the events held), `prepare` and the bidders' leave-alone. The harm records are planted on a stub of
`ContactHarm`; the traits come from the customers' own events. Each test well under a second."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import numpy as np

from autonomy.contracts import Channel, CustomerState, DecisionContext, Event, EventType
from autonomy.decide.bidders import ContactHarm, HarmFit, leave_alone_retry
from autonomy.decide.who import WhoBook, WhoConfig
from autonomy.fakes.samples import sample_brief
from tests.v3.who.test_bidders import bidder, state
from tests.v3.who.test_trait_harm import Records, record

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
TRAITS_ONLY = WhoConfig(models=False, risk_urgency=0.0, value_cost_share=None)

GROUPS = {
    # name: (customers, true ratio, needs a deal, messages in the last 30 days)
    "dog": (60, 0.4, True, True),
    "neutral": (60, 1.0, False, True),
    "deal_light": (150, 1.3, True, False),
    "full_light": (120, 1.2, False, False),
}


class PlantedHarm(ContactHarm):
    """A `ContactHarm` whose records are planted and whose fit is a marker (a new one is a refit)."""

    def __init__(self, records: Records) -> None:
        super().__init__()
        self.planted = records
        self.refit_marker()

    def refit_marker(self) -> None:
        self.fit = HarmFit((), 1.0, (), (), len(self.planted), len(self.planted))

    def own_records(self) -> Mapping[str, tuple[float, float, float]]:
        return self.planted


def history(cid: str, deals: bool, sends_before: datetime | None) -> list[Event]:
    """Six orders over the past year (with an offer for a deal buyer) and, when `sends_before` is
    given, six messages in the 30 days before it."""
    out = [
        Event(
            event_id=f"{cid}-p{k}",
            customer_id=cid,
            type=EventType.PURCHASE,
            ts=NOW - timedelta(days=40 + 50 * k),
            source="test",
            value=40.0,
            margin=16.0,
            attrs={"offer_id": "ten_off"} if deals else {},
        )
        for k in range(6)
    ]
    if sends_before is not None:
        out += sends(cid, sends_before)
    return out


def sends(cid: str, before: datetime) -> list[Event]:
    return [
        Event(
            event_id=f"{cid}-s-{before.date()}-{k}",
            customer_id=cid,
            type=EventType.SEND,
            ts=before - timedelta(days=2 + 4 * k),
            source="test",
            channel=Channel.EMAIL,
        )
        for k in range(6)
    ]


def planted() -> tuple[Records, dict[str, list[Event]], dict[str, str]]:
    rng = np.random.default_rng(11)
    records: Records = {}
    events: dict[str, list[Event]] = {}
    group_of: dict[str, str] = {}
    for name, (n, ratio, deals, heavy) in GROUPS.items():
        for i in range(n):
            cid = f"{name}-{i:03d}"
            records[cid] = record(rng, ratio, max(int(rng.poisson(4)), 1))
            events[cid] = history(cid, deals, NOW if heavy else None)
            group_of[cid] = name
    return records, events, group_of


def decide(book: WhoBook, cid: str, at: datetime) -> bool:
    """Whether the bidder bids for this customer at all, on the state `prepare` makes."""
    s = state(cid)
    view = s.model_copy(update=book.prepare(s, at))
    return bool(bidder(book, prune_z=2.0).bids(view, DecisionContext(now=at, briefs=(sample_brief(at),))))


def share_left_alone(book: WhoBook, ids: list[str], at: datetime) -> float:
    asked = [c for c in ids if not leave_alone_retry(c, at)]
    return float(np.mean([not decide(book, c, at) for c in asked]))


def test_refresh_keys_the_groups_from_the_profile_and_the_bidder_leaves_the_planted_dogs_alone():
    records, events, group_of = planted()
    book = WhoBook(TRAITS_ONLY)
    book.ingest(events, NOW)
    book.refresh(NOW, PlantedHarm(records))
    assert book.traits.fitted
    assert set(book.traits.groups()) == {
        "silent|deals|heavy",
        "silent|full_price|heavy",
        "silent|deals|light",
        "silent|full_price|light",
    }
    dogs = [c for c, g in group_of.items() if g == "dog"]
    neutral = [c for c, g in group_of.items() if g == "neutral"]
    assert share_left_alone(book, dogs, NOW) >= 0.7
    assert share_left_alone(book, neutral, NOW) <= 0.05
    # keyed at decision time as at fit time
    assert {book._keys[c] for c in dogs} == {"silent|deals|heavy"}
    # a new customer with the dogs' traits and no record is read as one of them; with the neutral
    # group's traits they are bid for
    book.ingest({"new-neutral": history("new-neutral", False, NOW)}, NOW)
    assert decide(book, "new-neutral", NOW)
    coverage = book.coverage()
    assert coverage["trait_group"] == coverage["prepared"] > len(dogs) + len(neutral) - 10


def test_a_dog_left_alone_stays_left_alone_when_its_own_messages_stop():
    records, events, group_of = planted()
    harm = PlantedHarm(records)
    book = WhoBook(TRAITS_ONLY)
    book.ingest(events, NOW)
    book.refresh(NOW, harm)
    dogs = [c for c, g in group_of.items() if g == "dog"]
    neutral = [c for c, g in group_of.items() if g == "neutral"]
    before = share_left_alone(book, dogs, NOW)
    assert before >= 0.7
    # Forty days on. The dogs were left alone, so they had no message for a month: keyed afresh on
    # their load they would join the lightly contacted deal buyers (who are helped by messages). The
    # neutral group was messaged as before.
    later = NOW + timedelta(days=40)
    book.ingest({c: sends(c, later) for c in neutral}, later)
    harm.refit_marker()
    book.refresh(later, harm)
    dog = dogs[0]
    load = book._load(book.events[dog], later)
    from autonomy.profile import profile_for

    assert book.trait_keys.key(profile_for(book.events[dog], later), load) == "silent|deals|light"
    assert share_left_alone(book, dogs, later) >= before - 0.05
    assert share_left_alone(book, neutral, later) <= 0.05


def test_without_a_harm_reading_the_trait_reading_is_dropped_too():
    records, events, group_of = planted()
    harm = PlantedHarm(records)
    book = WhoBook(TRAITS_ONLY)
    book.ingest(events, NOW)
    book.refresh(NOW, harm)
    dogs = [c for c, g in group_of.items() if g == "dog"]
    assert share_left_alone(book, dogs, NOW) >= 0.7
    harm.fit = None  # too few customers now: ContactHarm reads nothing
    later = NOW + timedelta(days=1)
    book.refresh(later, harm)
    assert not book.traits.fitted
    assert share_left_alone(book, dogs, later) == 0.0


def test_explaining_stores_nothing():
    records, events, _ = planted()
    book = WhoBook(TRAITS_ONLY)
    book.ingest(events, NOW)
    book.refresh(NOW, PlantedHarm(records))
    update = book.prepare(state("dog-000"), NOW, remember=False)
    assert "features" in update
    assert not book._scored and not book._keys.get("neutral-000")
    assert book.coverage()["prepared"] == 0


def test_nightly_features_from_after_now_or_stale_are_not_read():
    book = WhoBook()
    fresh = {"buy_ordered": 1.0}
    future = CustomerState(
        customer_id="x", as_of=NOW, features=fresh, features_as_of=NOW + timedelta(hours=6)
    )
    assert book.features(future, NOW) is None
    stale = future.model_copy(update={"features_as_of": NOW - timedelta(days=10)})
    assert book.features(stale, NOW) is None
    ok = future.model_copy(update={"features_as_of": NOW - timedelta(days=1)})
    assert book.features(ok, NOW) == fresh
