"""The tools of Plan O wave 1 in M232's registry (the integration of M234, M237, M238, M240 and M241; DEC-1802 (t)).

Those milestones were built beside M232, so each declares its tools in its own shape with a `register(registry)`:
M234 `engine.doctor.readiness.register_readiness_tool`, M237 `engine.doctor.tools_files.register`, M238
`engine.doctor.tools_rows.register`, M240 `engine.doctor.tools_formats.register` and M241
`engine.doctor.tools_locale.register` (whose registry method is `register_spec`). :func:`register_wave_one` hands
each of them an adapter that turns the spec into a :class:`~engine.doctor.registry.DataTool`:

* **Number, kind and exposure come from the catalogue** (`engine.doctor.catalogue`), the design's §5.5, so the
  registry cannot drift from it. M240 declared `check_parser_loss` `aggregate`; the catalogue (and the registry's
  rule that only #36 and #69 are aggregate) makes it `session-only`, which is the stricter reading.
* **Role.** The spec's role, raised to an Analyst for a `session-only` tool: M232's rule that a result holding a
  cell, a row or a masked example needs an Analyst (DEC-1802 (c)). `check_readiness` is aggregate and a Viewer's.
* **Callers.** The rules and the person (the screen), never the AI: the chat's tool list is unchanged with the
  switch on, and the AI reaches these tools through the visit (M235), once M233's policy has their rule sets.
* **Policy kind** for each PROPOSE tool: `data.<tool name>`, M237's own names. M233 adds the rule sets (default
  deny without them).
* **Context.** Each module reads a small protocol; it is built here from the `DataToolContext`: the file being set
  up (`agent`) and the decided ID column for M238's and M241's tools, the case's files for M237's, the case's file
  store (`case_files`, M231's case) for M240's, and the caller's readiness inputs for M234's.
* **Refusals.** A module's own coded refusal (`JoinError`, `StackError`, `BuildDriveError`, `RowToolError`)
  becomes a `DataToolError` with the same code (422), so the caller reads it like any other.

M240's `read_workbook_sheets` and `find_excel_serial_dates` had no row in the design's catalogue in wave 1; M236
(DEC-1806) gave them rows #82 and #83 with the design, so :data:`UNCATALOGUED` is empty and both are registered.
`find_excel_serial_dates` declares the step kind it proposes (`parse_date`), so the registry decides its steps by
M233's `data.excel_serial_dates` rule set.

Nothing here changes what the product does while `data_doctor.unified` is off: the registry is reached only
through its callers, and none of these tools is offered to the AI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
from pydantic import BaseModel

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import RecipeStepKind
from engine.doctor.catalogue import row
from engine.doctor.registry import (
    BY_EXPOSURE,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
    Need,
)

if TYPE_CHECKING:
    from engine.doctor.contracts import Diagnosis
    from engine.doctor.locale import LocaleProfile
    from engine.doctor.readiness import CheckReadinessArgs, ReadinessInputs

__all__ = ["RULES_AND_SCREEN", "UNCATALOGUED", "WAVE_ONE_TOOLS", "register_wave_one"]

RULES_AND_SCREEN: Final[frozenset[str]] = frozenset({"rules", "person"})

UNCATALOGUED: Final[frozenset[str]] = frozenset()
"""Wave-1 tools with no row in the design's catalogue: none since M236 gave M240's two tools rows #82 and #83."""

WAVE_ONE_TOOLS: Final[tuple[str, ...]] = (
    "read_other_format",
    "check_parser_loss",
    "synonym_clusters",
    "impossible_values",
    "detect_units",
    "keep_latest_per_id",
    "normalise_key",
    "join_files",
    "stack_files",
    "build_dataset",
    "split_file",
    "detect_locale",
    "parse_indian_numbers",
    "indian_booleans",
    "detect_india_ids",
    "check_readiness",
    "read_workbook_sheets",
    "find_excel_serial_dates",
)
"""Every tool this module registers, in catalogue order."""

_RANK: Final[dict[Role, int]] = {Role.VIEWER: 0, Role.ANALYST: 1, Role.APPROVER: 2, Role.ADMIN: 3}


# ---------------------------------------------------------------------------
# What each module reads, built from the registry's context
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _FileView:
    """M238's `RowToolContext` and M241's `LocaleToolContext`: the file being set up, its ID and outcome."""

    frame: pd.DataFrame
    primary_key: str | None
    target: str | None
    max_failure_pct: float
    as_of: date
    locale_profile: LocaleProfile | None = None

    def today(self) -> date:
        return self.as_of


def _file_view(ctx: DataToolContext) -> _FileView:
    agent = ctx.require_agent()
    return _FileView(
        frame=agent.frame,
        primary_key=ctx.primary_key,
        target=agent.target,
        max_failure_pct=agent.config.agent.max_conversion_failure_pct,
        as_of=ctx.clock().date(),
        locale_profile=ctx.locale_profile,
    )


@dataclass
class _FilesView:
    """M237's `FilesContext`: the case's files (frames and names) and their diagnosis, read once."""

    ctx: DataToolContext
    _names: dict[str, str] = field(default_factory=dict)

    def _loaded(self, upload_id: str) -> Any:
        for loaded in self.ctx.files:
            if loaded.upload_id == upload_id:
                return loaded
        raise DataToolError("TOOL_INPUT_INVALID", "That file is not in this case.", status=422)

    def frame(self, upload_id: str) -> pd.DataFrame:
        frame: pd.DataFrame = self._loaded(upload_id).frame
        return frame

    def file_name(self, upload_id: str) -> str:
        return str(self._loaded(upload_id).file_name)

    @cached_property
    def _diagnosis(self) -> Diagnosis:
        from engine.config import get_roles
        from engine.doctor.contracts import Diagnosis
        from engine.doctor.diagnose import diagnose_file
        from engine.doctor.joins import how_files_fit
        from engine.doctor.tools import _config

        ctx = self.ctx
        config = _config(ctx)
        roles = ctx.roles if ctx.roles is not None else get_roles(ctx.root())
        files = list(ctx.files)
        diagnosed = tuple(diagnose_file(loaded, config, roles) for loaded in files)
        return how_files_fit(Diagnosis(files=diagnosed, use_cases=()), files, roles)

    def diagnosis(self) -> Diagnosis | None:
        return self._diagnosis if self.ctx.files else None

    def role_proposer(self) -> Any:
        """M236's `propose_role`, through the registry, as the build plan's `RoleProposer` (DEC-1806)."""
        from engine.doctor.tools_wrappers import ProposedRoles

        return ProposedRoles(self.ctx)


@dataclass(frozen=True)
class _ReadinessView:
    """M234's `ReadinessToolContext`: the caller's inputs for the case's prepared copy."""

    inputs: Callable[[CheckReadinessArgs], ReadinessInputs]

    def readiness_inputs(self, args: CheckReadinessArgs) -> ReadinessInputs:
        return self.inputs(args)


def _case_files(ctx: DataToolContext) -> Any:
    if ctx.case_files is None:  # unreachable through the registry: `needs` makes the tool unavailable
        raise DataToolError("TOOL_UNKNOWN", "This tool needs a case.", status=404)
    return ctx.case_files


def _readiness(ctx: DataToolContext) -> _ReadinessView:
    if ctx.readiness is None:  # unreachable through the registry: `needs` makes the tool unavailable
        raise DataToolError("TOOL_UNKNOWN", "This tool needs a case's prepared file.", status=404)
    return _ReadinessView(ctx.readiness)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------
def _coded(
    run: Callable[[Any, Any], BaseModel], view: Callable[[DataToolContext], Any]
) -> Callable[..., Any]:
    """`run` over the module's view of `ctx`; a module's coded refusal becomes a `DataToolError` (422)."""
    from engine.doctor.build_driver import BuildDriveError
    from engine.doctor.join_files import JoinError
    from engine.doctor.stack_files import StackError
    from engine.doctor.tools_rows import RowToolError

    def wrapped(parsed: BaseModel, ctx: DataToolContext) -> BaseModel:
        try:
            return run(parsed, view(ctx))
        except (JoinError, StackError, BuildDriveError, RowToolError) as exc:
            raise DataToolError(exc.code, exc.message, status=422) from exc

    return wrapped


def _role(declared: Role | str, exposure: Exposure) -> Role:
    role = Role(declared)
    if exposure is Exposure.SESSION_ONLY and _RANK[role] < _RANK[Role.ANALYST]:
        return Role.ANALYST
    return role


@dataclass
class _Adapter:
    """Takes any wave-1 spec (`register`, `register_spec`, or a call) and registers its `DataTool`."""

    registry: DataToolRegistry
    view: Callable[[DataToolContext], Any]
    needs: tuple[Need, ...]
    registered: list[str] = field(default_factory=list)

    def __call__(self, spec: Any) -> None:
        self.register(spec)

    def register_spec(self, spec: Any) -> None:
        self.register(spec)

    def register(self, spec: Any) -> None:
        if spec.name in UNCATALOGUED:
            return
        entry = row(spec.name)
        if entry is None or entry.kind is None:
            raise ValueError(f"{spec.name!r} has no tool row in the design's catalogue.")
        kind = DataToolKind(entry.kind)
        if str(getattr(spec, "kind", entry.kind)) != entry.kind:
            raise ValueError(f"{spec.name!r} is {spec.kind}, but the catalogue says {entry.kind}.")
        exposure = Exposure(entry.exposure)
        declared = getattr(spec, "role", Role.VIEWER)
        role = _role(declared, exposure)
        purpose = (
            "ask Data Doctor for a suggestion"
            if kind is DataToolKind.PROPOSE
            else "ask Data Doctor about the data"
        )
        given = getattr(spec, "wraps", None)
        wraps = given if isinstance(given, str) else ", ".join(given or ())
        self.registry.register(
            DataTool(
                number=entry.number,
                name=spec.name,
                kind=kind,
                description=spec.description,
                input_model=spec.input_model,
                output_model=spec.output_model,
                route_policy=RoutePolicy(role=role, action=f"doctor.{spec.name}", purpose=purpose),
                exposure=exposure,
                run=_coded(spec.run, self.view),
                callers=RULES_AND_SCREEN,
                needs=self.needs,
                max_output_bytes=BY_EXPOSURE,
                policy_kind=(
                    (getattr(spec, "policy_kind", None) or f"data.{spec.name}")
                    if kind is DataToolKind.PROPOSE
                    else None
                ),
                wraps=wraps or f"{spec.run.__module__}.{spec.name}",
                step_kinds=tuple(
                    RecipeStepKind(k)
                    for k in (getattr(spec, "step_kinds", ()) or getattr(spec, "proposes", ()))
                ),
            )
        )
        self.registered.append(spec.name)


def register_wave_one(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register the tools of M240, M238, M237, M241 and M234 in `registry`; their names, in catalogue order."""
    from engine.doctor import readiness, tools_files, tools_formats, tools_locale, tools_rows

    adapters = (
        (_Adapter(registry, _case_files, ("case_files",)), tools_formats.register),
        (_Adapter(registry, _file_view, ("agent",)), tools_rows.register),
        (_Adapter(registry, _FilesView, ("files",)), tools_files.register),
        (_Adapter(registry, _file_view, ("agent",)), tools_locale.register),
        (_Adapter(registry, _readiness, ("readiness",)), readiness.register_readiness_tool),
    )
    names: list[str] = []
    for adapter, register in adapters:
        register(adapter)
        names += adapter.registered
    return tuple(sorted(names, key=lambda name: row(name).number))  # type: ignore[union-attr]
