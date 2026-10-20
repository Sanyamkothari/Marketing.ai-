"""One router for all six engines, under `/v2/engines`. The integrator mounts `router`.

Each engine lives in its own module of `api.v2.engines` and exposes an `APIRouter` named `router`
with its own sub-prefix (`/doctor`, `/mandates`, `/decisions`, `/studio`, `/dispatch`,
`/measurement`). Engines are found by importing their module: one that does not exist yet (its
stream has not merged) is skipped, so this file needs no edit when it lands. A module that exists
but fails to import is a bug and is not hidden: the error propagates.

`GET /v2/engines` lists the six engines and whether each is mounted in this build.
"""

from __future__ import annotations

import importlib
from typing import Any

from fastapi import APIRouter

from autonomy.contracts import Contract

PREFIX = "/v2/engines"

ENGINES: tuple[tuple[str, str, str], ...] = (
    ("P1", "Data Doctor", "api.v2.engines.doctor"),
    ("P2", "Mandate & Catalogue", "api.v2.engines.mandates"),
    ("P3", "Decision Hub & Clearinghouse", "api.v2.engines.decisions"),
    ("P4", "Creative & Compliance Studio", "api.v2.engines.studio"),
    ("P5", "Dispatch Gateway", "api.v2.engines.dispatch"),
    ("P6", "Causal Incrementality Ledger", "api.v2.engines.measurement"),
)
"""(id, name, module) of each engine, in the order they are mounted."""


class EngineInfo(Contract):
    id: str
    name: str
    module: str
    mounted: bool


class EngineIndex(Contract):
    engines: tuple[EngineInfo, ...]


def _missing(exc: ModuleNotFoundError, module: str) -> bool:
    """True when the error is the engine module itself (or its package) not existing."""
    name = exc.name or ""
    return name == module or module.startswith(name + ".")


def load_engine_router(module: str) -> APIRouter | None:
    """The engine module's `router`, or None when the module (or its `router`) does not exist yet."""
    try:
        mod: Any = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if _missing(exc, module):
            return None
        raise
    found = getattr(mod, "router", None)
    return found if isinstance(found, APIRouter) else None


def build_router() -> APIRouter:
    """A fresh `/v2/engines` router with every engine that is present in this build."""
    engines_router = APIRouter(prefix=PREFIX, tags=["engines"])
    mounted: list[EngineInfo] = []
    for engine_id, name, module in ENGINES:
        sub = load_engine_router(module)
        if sub is not None:
            engines_router.include_router(sub)
        mounted.append(EngineInfo(id=engine_id, name=name, module=module, mounted=sub is not None))
    index = EngineIndex(engines=tuple(mounted))

    @engines_router.get("", response_model=EngineIndex)
    def list_engines() -> EngineIndex:
        """The six engines and which of them this build serves."""
        return index

    return engines_router


router = build_router()
