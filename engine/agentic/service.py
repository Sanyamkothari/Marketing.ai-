"""The one in-process entry point to the agent-safe core: a checked `ToolContext` (Plan K M132).

Everything that lets an agent in - the `/agentic` routes, the scenario harness, M133's MCP server, M139's
planner - builds its context here, so the switch is checked in one place (`AGENTIC_DISABLED` while
`agentic.enabled` is off) and every context carries the decision ledger, signed, at the policy version in
force.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from engine.access.roles import Principal
from engine.agentic.config import policy_version, require_enabled
from engine.agentic.ledger import DecisionLedger, Signer
from engine.agentic.registry import ToolContext

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from engine.measurement.campaign import CampaignStore
    from engine.settings import Settings
    from engine.storage import Storage

__all__ = ["agentic_context"]


def agentic_context(
    *,
    storage: Storage,
    principal: Principal,
    config_root: Path,
    platform_engine: Engine,
    signer: Signer,
    settings: Settings | None = None,
    campaigns: CampaignStore | None = None,
    actor: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ToolContext:
    """A `ToolContext` with the ledger attached; refuses with `AGENTIC_DISABLED` while the switch is off."""
    from engine.measurement.campaign import SqlCampaignStore
    from engine.utils.time import utc_now

    require_enabled(config_root)
    ledger = DecisionLedger(platform_engine, signer=signer, policy_version=policy_version(config_root))
    return ToolContext(
        storage=storage,
        principal=principal,
        config_root=config_root,
        clock=clock or utc_now,
        actor=actor or principal.user_id,
        platform_engine=platform_engine,
        campaigns=campaigns if campaigns is not None else SqlCampaignStore(platform_engine),
        settings=settings,
        ledger=ledger,
    )
