"""The weekly copy run: types, drafts, the Critic, the scoring model, and the live tests (plan-v3 decision 6).

One call to `CopyPlanner.run` does the whole week for one goal:

1. **Types.** Group the customers into 50-300 types (`autonomy.content.types`).
2. **Write.** For each type, largest first, the Creative writes `drafts_per_type` drafts, spread over
   the (offer, theme) cells the brief allows, with the type's plain label as the audience. Personal
   details are `{{slots}}` the dispatcher fills at send time (`first_name`, `top_category`,
   `recommended_product`, `store_name`); the model never sees a customer.
3. **Check.** Every draft goes through the Critic unchanged; only APPROVED drafts continue.
4. **Score.** The scoring model ranks a type's approved drafts when it has beaten theme labels on
   held-out drafts; until then the ranking is by theme-label rate (see `autonomy.content.scoring`).
5. **Test live.** The top `top_k` drafts of each type go into a `LiveTest`.

The cost guard comes first: a type is written only if all its drafts still fit under the week's cap
(`CopyConfig.write_cap`, by default `max_types x drafts_per_type`). When the cap is reached the
remaining (smaller) types are listed as skipped and keep using the v2 library; nothing half-written
is left behind. A model call that fails falls back to the Creative's fixed template copy, as in v2.

Type ids (`ct000`, largest first) are refitted every week, so the same id can name a different group
next week. Content ids and WhatsApp template names are therefore keyed by the type id, the ISO week,
a hash of the type's label and centre, and (in the Creative) a hash of the final wording.

With `CopyConfig.enabled` False (the default) `run` does nothing at all: no types, no model call, an
empty plan, so the agent's behaviour and numbers are exactly v2's.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from autonomy.agents.creative import OFFER_THEMES, Creative
from autonomy.agents.critic import BrandRules, Critic
from autonomy.brand.kit import BrandKit
from autonomy.brand.rules import offer_in_style
from autonomy.content.config import CopyConfig, CostGuard
from autonomy.content.live import LiveBoard
from autonomy.content.scoring import PastSend, ScoringModel, ScoringReport, tally_sends
from autonomy.content.types import CustomerType, CustomerTypes
from autonomy.contracts import (
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    OfferOption,
    Theme,
    stable_id,
)

TIE = 1e-12
"""Scores closer than this count as equal when picking the top drafts."""


@dataclass(frozen=True)
class TypePlan:
    """What one type got this week."""

    type_id: str
    label: str
    size: int
    written: tuple[ContentItem, ...]
    approved: tuple[ContentItem, ...]
    live: tuple[ContentItem, ...]
    scores: dict[str, float]


@dataclass
class WeeklyPlan:
    """The result of one weekly run.

    `customer_types` is the fitted grouping: the dispatcher calls `customer_types.assign(state)` at
    send time to find a customer's type, then `board.tests[type_id].assign(customer_id)` for the
    draft. `llm_calls` counts requests that reached the model (a JSON retry inside one request is not
    counted; cells written from template copy while the model was down are not counted)."""

    enabled: bool
    goal_id: str
    types: tuple[CustomerType, ...] = ()
    customer_types: CustomerTypes | None = None
    plans: list[TypePlan] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    writes_used: int = 0
    write_cap: int = 0
    llm_calls: int = 0
    rejected: int = 0
    held: int = 0
    scoring: ScoringReport | None = None
    scoring_used: bool = False
    board: LiveBoard | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.enabled:
            return "Per-type copy is off: v2 copy is in use."
        scoring = self.scoring.sentence() if self.scoring else "No scoring model fitted."
        return (
            f"{len(self.plans)} of {len(self.types)} customer types written: {self.writes_used} of "
            f"{self.write_cap} drafts in {self.llm_calls} model calls, {self.rejected} rejected by the "
            f"Critic, {self.held} held. {len(self.skipped)} types skipped. {scoring}"
        )


def select_top(ranked: Sequence[tuple[ContentItem, float]], k: int) -> list[ContentItem]:
    """The best `k` items of a ranking; among equal scores, a theme not picked yet comes first.

    Without that, a ranking by theme-label rate (every draft of a theme scores the same) would put
    three drafts of one theme in the live test and the test would learn nothing about themes."""
    remaining = list(ranked)
    picked: list[ContentItem] = []
    themes: set[Theme] = set()
    while remaining and len(picked) < k:
        top = max(score for _, score in remaining)
        tied = [p for p in remaining if top - p[1] <= TIE]
        choice = next((p for p in tied if p[0].theme not in themes), tied[0])
        remaining.remove(choice)
        picked.append(choice[0])
        themes.add(choice[0].theme)
    return picked


def _cell_plan(
    cells: Sequence[tuple[OfferOption, Theme]], drafts: int, per_call: int, offset: int
) -> list[tuple[OfferOption, Theme, int]]:
    """Which cells to ask, and for how many variants each, to get `drafts` drafts.

    The cell list is rotated by `offset` (a hash of the type) so different types start from different
    cells. About `drafts / per_call` cells are used, one model call each (never the same cell twice,
    which would repeat content ids), and the drafts are shared out as evenly as possible."""
    if not cells:
        return []
    used = min(math.ceil(drafts / per_call), len(cells))
    start = offset % len(cells)
    base, extra = divmod(drafts, used)
    return [(*cells[(start + j) % len(cells)], base + (1 if j < extra else 0)) for j in range(used)]


def week_key(now: datetime) -> str:
    """The ISO week of `now`, as in "2026w40"."""
    year, week, _ = now.isocalendar()
    return f"{year}w{week:02d}"


def type_scope(ctype: CustomerType, now: datetime) -> str:
    """The key per-type copy is filed under: type id, ISO week, and a hash of the type's label and centre.

    A type id alone is not enough, because next week's `ct000` is usually a different group."""
    return f"{ctype.type_id}_{week_key(now)}_{stable_id('k', ctype.label, ctype.traits)[2:8]}"


class CopyPlanner:
    """Runs the weekly copy plan for a goal. Holds the agents; keeps no state between runs."""

    def __init__(
        self,
        config: CopyConfig,
        creative: Creative,
        critic: Critic,
        scoring: ScoringModel | None = None,
    ) -> None:
        self._cfg = config
        self._creative = creative
        self._critic = critic
        self._scoring = scoring

    def run(
        self,
        brief: Brief,
        states: Sequence[CustomerState],
        *,
        rules: BrandRules,
        brand_voice: str,
        now: datetime,
        channel: Channel | None = None,
        past_sends: Iterable[PastSend] = (),
        kit: BrandKit | None = None,
    ) -> WeeklyPlan:
        """Plan one week of copy for `brief`'s goal from the customers in `states`.

        `past_sends` are our own earlier sends, one `PastSend` (item, state, sent_at, bought) per
        message, with the customer's state AS OF the send, never today's: today's state already holds
        the purchase the send caused. Rows with a state dated after the send are skipped and noted.
        They are placed in this week's types and train the scoring model. `channel` defaults to the
        envelope's first channel and must be one the envelope allows. The result's `board` holds the
        live tests, ready for `record` and `review_all`; `customer_types` places a customer in a type."""
        cfg = self._cfg
        plan = WeeklyPlan(enabled=cfg.enabled, goal_id=brief.goal_id, write_cap=cfg.write_cap)
        if not cfg.enabled or not states:
            return plan
        if channel is not None and channel not in brief.envelope.channels:
            raise ValueError(f"channel {channel.value} is not in the envelope of goal {brief.goal_id}")

        types = CustomerTypes.fit(states, cfg)
        plan.types = types.types
        plan.customer_types = types
        sizes = types.counts(states)
        offers = {o.offer_id: o for o in brief.envelope.offers}

        scoring = self._scoring or ScoringModel(cfg, offers=offers)
        tally = tally_sends(past_sends, types)
        if tally.late_states:
            plan.notes.append(
                f"{tally.late_states} past sends skipped: the customer state is dated after the send"
            )
        plan.scoring = scoring.fit(tally.cells, {t.type_id: t.traits for t in types.types})
        plan.scoring_used = scoring.usable

        in_style = [o for o in brief.envelope.offers if kit is None or offer_in_style(o, kit.rules)]
        plan.notes.extend(
            f"{o.offer_id}: the shop's own copy never states this kind of discount"
            for o in brief.envelope.offers
            if o not in in_style
        )
        cells = [(o, t) for o in in_style for t in OFFER_THEMES[o.kind]]
        if not cells:
            plan.notes.append("no offer and theme the brief allows: nothing written, v2 copy is in use")
            return plan
        use_channel = channel or brief.envelope.channels[0]
        guard = CostGuard(cfg.write_cap)
        board = LiveBoard(cfg)
        plan.board = board
        self._creative.reset()
        templated_cells = 0

        order = sorted(types.types, key=lambda t: (-sizes[t.type_id], t.type_id))
        for position, ctype in enumerate(order):
            if not guard.allow(cfg.drafts_per_type):
                plan.skipped.extend(
                    f"{t.type_id}: cost guard, {guard.used} of {guard.cap} drafts used"
                    for t in order[position:]
                )
                break
            before = len(self._creative.templated)
            written = self._write(brief, ctype, cells, use_channel, guard, brand_voice, now, kit)
            templated_cells += len(self._creative.templated) - before
            if not written:
                plan.skipped.append(f"{ctype.type_id}: nothing was written (see the notes)")
                continue
            reviewed = self._critic.review(written, rules)
            approved = tuple(i for i in reviewed if i.status is ContentStatus.APPROVED)
            plan.rejected += sum(i.status is ContentStatus.REJECTED for i in reviewed)
            plan.held += sum(i.status is ContentStatus.DRAFT for i in reviewed)
            if not approved:
                plan.skipped.append(f"{ctype.type_id}: no draft passed the Critic")
                continue
            ranked = scoring.rank(approved, ctype.type_id)
            live = select_top(ranked, cfg.top_k)
            board.start(ctype.type_id, live)
            plan.plans.append(
                TypePlan(
                    type_id=ctype.type_id,
                    label=ctype.label,
                    size=sizes[ctype.type_id],
                    written=tuple(reviewed),
                    approved=approved,
                    live=tuple(live),
                    scores={i.content_id: s for i, s in ranked},
                )
            )
        plan.writes_used = guard.used
        plan.llm_calls = guard.calls - templated_cells
        plan.notes.extend([*self._creative.dropped, *self._creative.failures, *self._creative.templated])
        return plan

    def _write(
        self,
        brief: Brief,
        ctype: CustomerType,
        cells: Sequence[tuple[OfferOption, Theme]],
        channel: Channel,
        guard: CostGuard,
        brand_voice: str,
        now: datetime,
        kit: BrandKit | None,
    ) -> list[ContentItem]:
        offset = int(stable_id("t", self._cfg.seed, ctype.type_id)[2:10], 16)
        scope = type_scope(ctype, now)
        out: list[ContentItem] = []
        for offer, theme, n in _cell_plan(
            cells, self._cfg.drafts_per_type, self._cfg.drafts_per_call, offset
        ):
            guard.spend(n)
            out.extend(
                self._creative.write_for_type(
                    brief,
                    type_id=scope,
                    type_note=ctype.note,
                    channel=channel,
                    offer=offer,
                    theme=theme,
                    n=n,
                    brand_voice=brand_voice,
                    now=now,
                    kit=kit,
                )
            )
        return out
