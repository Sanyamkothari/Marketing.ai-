"""Unified tool specification for Data Doctor tools across wave modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from engine.access.roles import Role
from engine.agent.contracts import RecipeStepKind
from engine.doctor.registry import Exposure

In = TypeVar("In")
Out = TypeVar("Out")
ToolKindName = Literal["read", "propose", "check", "decide"]


ExposureType = Exposure | Literal["session-only", "aggregate"] | str


@dataclass(frozen=True)
class DoctorToolSpec(Generic[In, Out]):
    """One Data Doctor tool specification in the unified shape across wave modules."""

    name: str
    kind: ToolKindName | str = "read"
    mode: Literal["rules", "ai", "rules + ai", "rules+ai"] = "rules"
    description: str = ""
    input_model: type[In] = BaseModel  # type: ignore[assignment]
    output_model: type[Out] = BaseModel  # type: ignore[assignment]
    exposure: ExposureType = Exposure.SESSION_ONLY
    role: Role | str = Role.VIEWER
    wraps: str | tuple[str, ...] = ()
    deterministic: bool = True
    proposes: tuple[str, ...] = ()
    step_kinds: tuple[RecipeStepKind | str, ...] = ()
    approve_class: bool | str = False
    person_only: bool = False
    policy_kind: str | None = None
    mirrors: tuple[str, ...] = ()
    probe_codes: tuple[str, ...] = ()
    run: Callable[[In, Any], Out] = None  # type: ignore[assignment]


__all__ = ["DoctorToolSpec", "ToolKindName"]
