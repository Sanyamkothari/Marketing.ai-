"""Live tests: the top drafts of each type go out together, and a clear loser is cut (plan-v3 decision 6, step 5).

Per customer type, the top `top_k` drafts (three by default) are sent in equal shares: a customer's
draft is a fixed hash of the type and the customer, so nobody gets a different draft each week, a cut
moves only the customers of the cut draft, and the share of every live draft is known exactly
(`LiveTest.probability`, logged with the decision like every other choice the agent makes). Only
agent-group customers are assigned; the holdout never enters a live test.

A draft is cut only under a conservative rule, because cutting a good draft by luck costs more than
sending a weak one a little longer:

* it needs at least `min_sends_per_arm` sends, and so does the leader (the live draft with the highest
  rate among those that qualify);
* its Wilson confidence band, `cut_z` standard errors wide (3 by default), must lie entirely below
  the leader's band, which is a wider gap than a z-test at that width would ask for;
* the leader is never cut, so at least one draft stays live.

Looking often does not defeat this, because the bar is high at every look: for two equal drafts the
chance that the bands separate at a single look is far below 0.1%, and the tests run many looks over
many seeds to check that. The cost is patience: a draft that is only a little worse stays live.

What the live test learns improves next week's ranking through the per-send rows: store each send
with the customer's state as of the send (`autonomy.content.scoring.PastSend`) and pass them to next
week's `CopyPlanner.run` as `past_sends`. `LiveBoard.feedback` is only this week's summary.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from autonomy.content.config import CopyConfig
from autonomy.content.scoring import DraftResult
from autonomy.contracts import ContentItem


def wilson_bounds(purchases: int, sends: int, z: float) -> tuple[float, float]:
    """The Wilson score interval for a rate, `z` standard errors wide. (0, 1) when there are no sends."""
    if sends <= 0:
        return 0.0, 1.0
    p = purchases / sends
    denom = 1.0 + z * z / sends
    centre = (p + z * z / (2.0 * sends)) / denom
    half = z * math.sqrt(p * (1.0 - p) / sends + z * z / (4.0 * sends * sends)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Arm:
    """One live draft in one type."""

    item: ContentItem
    sends: int = 0
    purchases: int = 0
    live: bool = True
    cut_reason: str | None = None

    @property
    def rate(self) -> float:
        return self.purchases / self.sends if self.sends else 0.0


@dataclass(frozen=True)
class Cut:
    """A draft taken out of a live test, with the numbers that justified it."""

    type_id: str
    content_id: str
    rate: float
    leader_id: str
    leader_rate: float
    sends: int
    reason: str


@dataclass
class LiveTest:
    """The live test of one type: its drafts, their results, and the rule that cuts a loser."""

    type_id: str
    config: CopyConfig
    arms: dict[str, Arm] = field(default_factory=dict)

    @classmethod
    def start(cls, type_id: str, items: Sequence[ContentItem], config: CopyConfig) -> LiveTest:
        """A test of `items` (at most `config.top_k`, in the order given)."""
        if not items:
            raise ValueError("a live test needs at least one draft")
        test = cls(type_id=type_id, config=config)
        for item in items[: config.top_k]:
            test.arms[item.content_id] = Arm(item=item)
        return test

    def live_ids(self) -> list[str]:
        return [cid for cid, arm in self.arms.items() if arm.live]

    @property
    def winner(self) -> str | None:
        """The one draft left, once the others are cut; None while more than one is live."""
        live = self.live_ids()
        return live[0] if len(live) == 1 else None

    def probability(self, content_id: str) -> float:
        """The chance a customer of this type is sent this draft (0 once it is cut)."""
        live = self.live_ids()
        return 1.0 / len(live) if content_id in live else 0.0

    def assign(self, customer_id: str, seed: int = 0) -> str:
        """The live draft this customer gets: a fixed, equal-share split by hash.

        The customer's slot is taken over all the test's drafts, cut or not, so a cut never moves
        the customers of a surviving draft. Only the customers of a cut draft are moved, by a second
        hash over the drafts still live, which keeps the shares equal."""
        arms = list(self.arms)
        digest = hashlib.sha256(f"{seed}|{self.type_id}|{customer_id}".encode()).hexdigest()
        first = arms[int(digest[:12], 16) % len(arms)]
        if self.arms[first].live:
            return first
        live = self.live_ids()
        return live[int(digest[12:24], 16) % len(live)]

    def record(self, content_id: str, sends: int, purchases: int) -> None:
        """Add results for a live draft. Results for a draft already cut are kept but change nothing."""
        if content_id not in self.arms:
            raise KeyError(f"{content_id} is not in the live test of {self.type_id}")
        if sends < 0 or not 0 <= purchases <= sends:
            raise ValueError("purchases must be between 0 and sends")
        arm = self.arms[content_id]
        arm.sends += sends
        arm.purchases += purchases

    def review(self) -> list[Cut]:
        """Cut every live draft that is clearly behind the leader. Returns the cuts made now."""
        cfg = self.config
        qualified = [a for a in self.arms.values() if a.live and a.sends >= cfg.min_sends_per_arm]
        if len(self.live_ids()) < 2 or len(qualified) < 2:
            return []
        leader = max(qualified, key=lambda a: (a.rate, a.item.content_id))
        lead_low = wilson_bounds(leader.purchases, leader.sends, cfg.cut_z)[0]
        cuts: list[Cut] = []
        for arm in qualified:
            if arm is leader:
                continue
            high = wilson_bounds(arm.purchases, arm.sends, cfg.cut_z)[1]
            if high < lead_low:
                arm.live = False
                arm.cut_reason = (
                    f"rate {arm.rate:.2%} on {arm.sends} sends; even its best case ({high:.2%}) is below "
                    f"the leader's worst case ({lead_low:.2%}, rate {leader.rate:.2%} on {leader.sends})"
                )
                cuts.append(
                    Cut(
                        self.type_id,
                        arm.item.content_id,
                        arm.rate,
                        leader.item.content_id,
                        leader.rate,
                        arm.sends,
                        arm.cut_reason,
                    )
                )
        return cuts


class LiveBoard:
    """The live tests of every type for one goal."""

    def __init__(self, config: CopyConfig) -> None:
        self._cfg = config
        self.tests: dict[str, LiveTest] = {}

    def start(self, type_id: str, items: Sequence[ContentItem]) -> LiveTest:
        test = LiveTest.start(type_id, items, self._cfg)
        self.tests[type_id] = test
        return test

    def record(self, type_id: str, content_id: str, sends: int, purchases: int) -> None:
        self.tests[type_id].record(content_id, sends, purchases)

    def review_all(self) -> list[Cut]:
        """Review every type's test (in type order) and return all cuts made."""
        return [cut for tid in sorted(self.tests) for cut in self.tests[tid].review()]

    def feedback(self) -> list[DraftResult]:
        """This week's results so far, per (draft, type), as a same-week summary.

        Not training data for next week: the type ids are this week's and are refitted. The scoring
        model learns from the per-send rows (`PastSend`, with the state as of the send), which the
        planner places in next week's types."""
        return [
            DraftResult(item=arm.item, type_id=tid, sends=arm.sends, purchases=arm.purchases)
            for tid in sorted(self.tests)
            for arm in self.tests[tid].arms.values()
            if arm.sends > 0
        ]

    def record_many(self, rows: Iterable[tuple[str, str, int, int]]) -> None:
        """(type id, content id, sends, purchases) rows, as a dispatcher's weekly tally would give them."""
        for type_id, content_id, sends, purchases in rows:
            self.record(type_id, content_id, sends, purchases)

    def live_by_type(self) -> Mapping[str, list[str]]:
        return {tid: t.live_ids() for tid, t in sorted(self.tests.items())}
