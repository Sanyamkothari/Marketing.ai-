"""Reading a stored artefact for a tool or the policy: missing is not the same as unreadable (Plan K M132).

A tool answers "not computed yet" with `null` and a plain reason only when the artefact is **not there**. An
artefact that is there but does not read - a partial write, a file from a schema that has since changed - is
not "not measured" or "not recorded": saying so would be a false sentence. It is refused with
`TOOL_ARTEFACT_UNREADABLE`, naming the file, so whoever asked learns the real reason.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from engine.agentic.errors import AgenticError

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = ["read_stored_json", "read_stored_model"]

M = TypeVar("M", bound=BaseModel)


def _unreadable(key: str, exc: Exception) -> AgenticError:
    parts = key.split("/")
    name, owner = parts[-1], (parts[-2] if len(parts) > 1 else "")
    return AgenticError(
        "TOOL_ARTEFACT_UNREADABLE",
        f"{name} for {owner} is stored but could not be read ({type(exc).__name__}); it was not used. "
        "Recompute it through the route that writes it.",
        status=500,
        detail={"file": name, "object_id": owner},
    )


def read_stored_model(storage: Storage, key: str, model: type[M]) -> M | None:
    """`model` read from `key`; None when nothing is stored there; `TOOL_ARTEFACT_UNREADABLE` when it is
    stored but does not read or validate."""
    from engine.storage import StorageError

    if not storage.exists(key):
        return None
    try:
        return storage.read_model(key, model)
    except (StorageError, OSError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        raise _unreadable(key, exc) from exc


def read_stored_json(storage: Storage, key: str) -> dict[str, Any] | None:
    """The JSON object at `key`; None when nothing is stored there; `TOOL_ARTEFACT_UNREADABLE` when it is
    stored but is not a JSON object."""
    from engine.storage import StorageError

    if not storage.exists(key):
        return None
    try:
        loaded = json.loads(storage.read_bytes(key))
    except (StorageError, OSError, ValueError) as exc:
        raise _unreadable(key, exc) from exc
    if not isinstance(loaded, dict):
        raise _unreadable(key, ValueError("not a JSON object"))
    return loaded
