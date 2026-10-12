"""The locale tools' registry entries (Plan O M241; the design §5.2 and §5.5 G #60-#63), standalone.

M232's registry (`engine/doctor/registry.py`) is built on another branch, so this module declares each tool as a
plain `LocaleToolSpec` shaped like the design's `DataTool` entry - name, kind, mode, input and output models (both
`extra="forbid"`), exposure, the role a caller needs, the engine function it wraps, deterministic or not - with a
`run(args, ctx)` handler, and a `register(registry)` the integrator wires to M232's registry.

| Tool | Kind | Exposure | Role | Wraps |
|---|---|---|---|---|
| `detect_locale` | READ | session-only | viewer | `engine.doctor.locale.detect_locale` |
| `parse_indian_numbers` | PROPOSE | session-only | analyst | `engine.doctor.indian_values.propose_indian_numbers` |
| `indian_booleans` | PROPOSE | session-only | analyst | `engine.doctor.indian_values.propose_indian_booleans` |
| `detect_india_ids` | READ | session-only | viewer | `engine.doctor.india_ids.detect_india_ids` |

All four are rules only and deterministic; none needs an AI service. The READ tools return counts only. The PROPOSE
tools return **pending** steps of the fixed vocabulary (`parse_number`, `map_boolean`) with masked examples - that is
why they are session-only (O7: no Data Doctor tool is listed on MCP). A PROPOSE tool adds nothing by itself: M232's
registry turns each returned step into a pending case item, and M233's policy rule set (listed in the DEC) decides
whether it may be offered at all. No tool here applies anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from engine.access.roles import Role
from engine.doctor.india_ids import detect_india_ids
from engine.doctor.indian_values import propose_indian_booleans, propose_indian_numbers
from engine.doctor.locale import LocaleProfile, detect_locale

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "LOCALE_TOOLS",
    "DetectIndiaIdsInput",
    "DetectIndiaIdsOutput",
    "DetectLocaleInput",
    "DetectLocaleOutput",
    "LocaleProposalsOutput",
    "LocaleToolContext",
    "LocaleToolSpec",
    "ProposeLocaleStepsInput",
    "register",
]

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class LocaleToolContext(Protocol):
    """The smallest part of M232's `DataToolContext` these tools read: the file, its ID and outcome, the profile."""

    @property
    def frame(self) -> pd.DataFrame: ...

    @property
    def primary_key(self) -> str | None: ...

    @property
    def target(self) -> str | None: ...

    @property
    def locale_profile(self) -> LocaleProfile | None: ...


# ---------------------------------------------------------------------------
# Input and output models
# ---------------------------------------------------------------------------
class DetectLocaleInput(_Strict):
    """No arguments: the whole file is read."""


class DetectLocaleOutput(_Strict):
    profile: dict[str, Any] = Field(
        description="Each setting's value and source (`default`, `detected`, `confirmed`)."
    )
    conflicts: list[dict[str, Any]] = Field(default_factory=list, description="Fields the file contradicts.")
    unproven: list[str] = Field(default_factory=list, description="Fields the file needs but does not prove.")
    columns_read: int = 0


class ProposeLocaleStepsInput(_Strict):
    columns: list[str] | None = Field(
        default=None, max_length=200, description="Columns to look at; every text column when left out."
    )


class LocaleProposalsOutput(_Strict):
    proposals: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per column: the pending step (or none), confidence, counts, masked examples.",
    )


class DetectIndiaIdsInput(_Strict):
    columns: list[str] | None = Field(default=None, max_length=200)


class DetectIndiaIdsOutput(_Strict):
    findings: list[dict[str, Any]] = Field(
        default_factory=list, description="Per column: the ID kind and counts."
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _run_detect_locale(_args: DetectLocaleInput, ctx: LocaleToolContext) -> DetectLocaleOutput:
    detection = detect_locale(ctx.frame, base=ctx.locale_profile)
    profile = detection.profile
    return DetectLocaleOutput(
        profile={
            name: {"value": setting.value, "source": setting.source, "evidence": list(setting.evidence)}
            for name, setting in ((n, getattr(profile, n)) for n in profile.values())
        },
        conflicts=[c.model_dump(mode="json") for c in detection.conflicts],
        unproven=list(detection.unproven),
        columns_read=detection.columns_read,
    )


def _run_numbers(args: ProposeLocaleStepsInput, ctx: LocaleToolContext) -> LocaleProposalsOutput:
    found = propose_indian_numbers(
        ctx.frame,
        columns=args.columns,
        primary_key=ctx.primary_key,
        target=ctx.target,
        profile=ctx.locale_profile,
    )
    return LocaleProposalsOutput(proposals=[p.as_json() for p in found])


def _run_booleans(args: ProposeLocaleStepsInput, ctx: LocaleToolContext) -> LocaleProposalsOutput:
    found = propose_indian_booleans(
        ctx.frame,
        columns=args.columns,
        primary_key=ctx.primary_key,
        target=ctx.target,
        profile=ctx.locale_profile,
    )
    return LocaleProposalsOutput(proposals=[p.as_json() for p in found])


def _run_ids(args: DetectIndiaIdsInput, ctx: LocaleToolContext) -> DetectIndiaIdsOutput:
    return DetectIndiaIdsOutput(
        findings=[f.as_json() for f in detect_india_ids(ctx.frame, columns=args.columns)]
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------
ToolKindName = Literal["read", "propose", "ask"]
Exposure = Literal["session-only", "aggregate"]


@dataclass(frozen=True)
class LocaleToolSpec(Generic[In, Out]):
    """One registry entry, as the design §5.2 shapes it."""

    name: str
    kind: ToolKindName
    mode: Literal["rules", "ai", "rules + ai"]
    description: str
    input_model: type[In]
    output_model: type[Out]
    exposure: Exposure
    role: Role
    wraps: str
    deterministic: bool
    run: Callable[[In, LocaleToolContext], Out]
    proposes: tuple[str, ...] = ()
    """For PROPOSE: the recipe step kinds it may return (what M233's policy rule set allows)."""


LOCALE_TOOLS: Final[tuple[LocaleToolSpec[Any, Any], ...]] = (
    LocaleToolSpec(
        name="detect_locale",
        kind="read",
        mode="rules",
        description="How the file writes numbers, dates, yes/no, phones and IDs; each setting with its source.",
        input_model=DetectLocaleInput,
        output_model=DetectLocaleOutput,
        exposure="session-only",
        role=Role.VIEWER,
        wraps="engine.doctor.locale.detect_locale",
        deterministic=True,
        run=_run_detect_locale,
    ),
    LocaleToolSpec(
        name="parse_indian_numbers",
        kind="propose",
        mode="rules",
        description="Propose reading lakh, crore, K, M and a trailing /- as numbers, per text column.",
        input_model=ProposeLocaleStepsInput,
        output_model=LocaleProposalsOutput,
        exposure="session-only",
        role=Role.ANALYST,
        wraps="engine.doctor.indian_values.propose_indian_numbers",
        deterministic=True,
        run=_run_numbers,
        proposes=("parse_number",),
    ),
    LocaleToolSpec(
        name="indian_booleans",
        kind="propose",
        mode="rules",
        description="Propose reading Hindi and Hinglish yes and no (haan, nahi, हाँ, नहीं), only when both are seen.",
        input_model=ProposeLocaleStepsInput,
        output_model=LocaleProposalsOutput,
        exposure="session-only",
        role=Role.ANALYST,
        wraps="engine.doctor.indian_values.propose_indian_booleans",
        deterministic=True,
        run=_run_booleans,
        proposes=("map_boolean",),
    ),
    LocaleToolSpec(
        name="detect_india_ids",
        kind="read",
        mode="rules",
        description="Columns holding a GSTIN (with its check character), a voter ID, a PIN code or a mobile number.",
        input_model=DetectIndiaIdsInput,
        output_model=DetectIndiaIdsOutput,
        exposure="session-only",
        role=Role.VIEWER,
        wraps="engine.doctor.india_ids.detect_india_ids",
        deterministic=True,
        run=_run_ids,
    ),
)


class LocaleRegistry(Protocol):
    """What `register` needs of M232's registry: one method that takes a spec. The integrator adapts the spec to
    M232's `DataTool` (route policy from `role`, evidence label, output budget) inside it."""

    def register_spec(self, spec: LocaleToolSpec[Any, Any]) -> None: ...


def register(registry: LocaleRegistry) -> None:
    """Add the four locale tools to `registry`, in catalogue order (#60-#63)."""
    for spec in LOCALE_TOOLS:
        registry.register_spec(spec)
