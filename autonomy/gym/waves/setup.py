"""What a waves run is made of: the brief and content the policies get, the scenario, the world factory.

The brief lets a policy reach anyone by SMS or e-mail with no offer (the benchmark's messages carry no
discount), at most twice a week, outside 21:00-09:00 shop time, with a fixed 10% holdout and nobody on the
Gym's baseline rule: every non-holdout customer is the policy's to decide.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from autonomy.contracts import (
    NO_OFFER,
    Audience,
    Brief,
    Channel,
    ContentItem,
    ContentStatus,
    Envelope,
    Format,
    Objective,
    Theme,
    Tone,
)
from autonomy.gym.api import ScenarioSpec
from autonomy.gym.score import SimWorld, WorldFactory
from autonomy.gym.waves.config import (
    CHANNEL_COST,
    EFFECT_DAYS,
    HOLDOUT_SHARE,
    HORIZON_DAYS,
    WEEKLY_CAP,
    WorldSpec,
)
from autonomy.gym.waves.data import WavesData

GOAL_ID = "goal_waves"
EPOCH = datetime(2018, 12, 1, tzinfo=UTC)
CHANNELS_ALLOWED: tuple[Channel, ...] = (Channel.SMS, Channel.EMAIL)


def waves_brief(cap: int = WEEKLY_CAP) -> Brief:
    """The approved brief: everyone eligible, SMS or e-mail, no offers, `cap` contacts a week, 14-day outcomes."""
    return Brief(
        brief_id=f"brief_{GOAL_ID}_1",
        goal_id=GOAL_ID,
        objective=Objective.GROSS_PROFIT,
        audience=Audience(description="Every customer"),
        horizon_days=EFFECT_DAYS,
        envelope=Envelope(
            channels=CHANNELS_ALLOWED,
            offers=(NO_OFFER,),
            budget=1e12,
            max_contacts_per_week=cap,
            quiet_start_hour=21,
            quiet_end_hour=9,
            holdout_share=HOLDOUT_SHARE,
            baseline_share=0.0,
        ),
        rationale="Waves benchmark: the policy decides who is worth a message; nobody is pre-selected.",
        created_by="waves",
        created_at=EPOCH,
        approved_by="waves",
        approved_at=EPOCH,
    )


def waves_content() -> list[ContentItem]:
    """An approved library with a few themes per channel (they carry no effect in any world)."""
    rows: list[tuple[Channel, Theme, str | None, str]] = [
        (Channel.SMS, Theme.REMINDER, None, "Points are waiting for you."),
        (Channel.SMS, Theme.LOYALTY, None, "Thank you for shopping with us."),
        (Channel.EMAIL, Theme.REMINDER, "Something new for you", "New in your usual aisles."),
        (Channel.EMAIL, Theme.EDUCATION, "Three ideas for the week", "Recipes and tips."),
        (Channel.EMAIL, Theme.LOYALTY, "Thank you for staying with us", "A note for regulars."),
    ]
    return [
        ContentItem(
            content_id=f"waves_{ch.value}_{theme.value}",
            goal_id=GOAL_ID,
            channel=ch,
            format=Format.TEXT,
            theme=theme,
            tone=Tone.FRIENDLY,
            offer_id=NO_OFFER.offer_id,
            subject=subject,
            body=body,
            status=ContentStatus.APPROVED,
            created_at=EPOCH,
        )
        for ch, theme, subject, body in rows
    ]


def waves_spec(world: WorldSpec, data: WavesData, *, seed: int = 0, days: int = HORIZON_DAYS) -> ScenarioSpec:
    """The Gym scenario record of a waves world: its name, customers, days and message costs."""
    return ScenarioSpec(
        name=f"waves_{world.name}",
        description=world.description,
        seed=seed,
        n_customers=max(data.n, 10),
        history_days=0,
        days=days,
        effect_days=EFFECT_DAYS,
        channel_cost=dict(CHANNEL_COST),
        margin_rate=0.25,
    )


def world_factory(data: WavesData, world: WorldSpec, holder: list[SimWorld] | None = None) -> WorldFactory:
    """A Gym `WorldFactory` building the waves world for a scenario. Each world it builds is appended to
    `holder` (when given), so the caller can read its ledger after `run_policy` returns."""
    from autonomy.gym.waves.world import WavesWorld

    def make(
        spec: ScenarioSpec,
        briefs: Sequence[Brief],
        content: Sequence[ContentItem],  # noqa: ARG001
    ) -> SimWorld:
        envelope = briefs[0].envelope if briefs else None
        built = WavesWorld(
            spec,
            data,
            world,
            contact_cap=envelope.max_contacts_per_week if envelope is not None else WEEKLY_CAP,
            quiet_start_hour=envelope.quiet_start_hour if envelope is not None else 21,
            quiet_end_hour=envelope.quiet_end_hour if envelope is not None else 9,
        )
        if holder is not None:
            holder.append(built)  # type: ignore[arg-type]
        return built  # type: ignore[return-value]

    return make
