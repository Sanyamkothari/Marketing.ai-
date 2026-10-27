"""The tools of Plan O wave 2 in M232's registry that came as specs (the integration of M242; DEC-1812 (k)).

M242 (currency) declared its tools in M241's spec shape (`engine.doctor.tools_currency.CURRENCY_TOOLS`) with a
`register(registry)` whose registry method is `register_spec`. :func:`register_wave_two` hands it wave 1's adapter
(`engine.doctor.tools_wave_one._Adapter`), so the rules are the same: number, kind and exposure from the design's
catalogue, the role raised to an Analyst for a `session-only` tool, the policy kind `data.<tool name>`, never the
AI as a caller.

* **`convert_currency` (#64)** is registered. It is person-only (the rate, its date and its source are the
  person's words), so its only caller is the person (the screen), and its proposal is APPROVE-class: M233's rule
  set `data.convert_currency` (`configs/agentic/policy.yaml`) refuses it without the exact sentence, and M246's
  `approve_class_of` asks an Analyst's own yes for it.
* **`check_currency` and `report_in_currency`** have no row in the design's catalogue (§5.5 G has #64 only), so
  they are not registered (:data:`UNCATALOGUED`); the visit can still call `engine.doctor.currency` directly. Giving
  them rows is a design change (`docs/CROSS_BRANCH_REQUESTS.md`), as M236 did for M240's #82 and #83.

The registry is reached only through its callers, and none of these tools is offered to the AI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pandas as pd

from engine.doctor.catalogue import row
from engine.doctor.registry import DataToolContext, DataToolRegistry
from engine.doctor.tools_wave_one import _Adapter

if TYPE_CHECKING:
    from engine.doctor.locale import LocaleProfile

__all__ = ["UNCATALOGUED", "WAVE_TWO_TOOLS", "register_wave_two"]

UNCATALOGUED: Final[frozenset[str]] = frozenset({"check_currency", "report_in_currency"})
"""M242's tools with no row in the design's catalogue: not registered until the design gives them one."""

WAVE_TWO_TOOLS: Final[tuple[str, ...]] = ("convert_currency",)
"""Every tool this module registers, in catalogue order."""


@dataclass(frozen=True)
class _CurrencyView:
    """M242's `CurrencyToolContext`: the file being set up, its ID and outcome, the client's locale profile and the
    workspace currency (`engine.doctor.currency.workspace_currency_for`: the client's confirmed one, else the
    workspace setting, else INR)."""

    frame: pd.DataFrame
    primary_key: str | None
    target: str | None
    locale_profile: LocaleProfile | None
    workspace_currency: str


def _currency_view(ctx: DataToolContext) -> _CurrencyView:
    from engine.doctor.currency import workspace_currency_for

    agent = ctx.require_agent()
    return _CurrencyView(
        frame=agent.frame,
        primary_key=ctx.primary_key,
        target=agent.target,
        locale_profile=ctx.locale_profile,
        workspace_currency=workspace_currency_for(agent.config.data_doctor, ctx.locale_profile).code,
    )


def register_wave_two(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register M242's catalogued tools in `registry`; their names, in catalogue order."""
    from engine.doctor import tools_currency

    adapter = _Adapter(registry, _currency_view, ("agent",), uncatalogued=UNCATALOGUED)
    tools_currency.register(adapter)
    return tuple(sorted(adapter.registered, key=lambda name: row(name).number))  # type: ignore[union-attr]
