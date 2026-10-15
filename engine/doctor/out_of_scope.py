"""The sentences Data Doctor says when a situation is outside this version of the product.

`configs/doctor/out_of_scope.yaml` holds them by code (docs/DATA_DOCTOR_SCENARIOS.md, section 4), so the
agent, the rules and the screens say the same words about the same situation. `out_of_scope` returns the
sentence with its blanks filled; `out_of_scope_request` names the forwardable data request to offer with
it (`engine.pilot.data_request.render_request_message`), when the situation has one.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from engine.config import load_yaml

__all__ = [
    "OUT_OF_SCOPE_PATH",
    "OutOfScope",
    "load_out_of_scope",
    "out_of_scope",
    "out_of_scope_codes",
    "out_of_scope_request",
]

OUT_OF_SCOPE_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "configs" / "doctor" / "out_of_scope.yaml"
)


class OutOfScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    situation: str
    say: str
    request: str | None = None


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    sentences: dict[str, OutOfScope]


@lru_cache(maxsize=4)
def _load(path: Path, mtime_ns: int) -> dict[str, OutOfScope]:
    del mtime_ns
    return dict(_File.model_validate(load_yaml(path)).sentences)


def load_out_of_scope(path: Path | None = None) -> dict[str, OutOfScope]:
    """Every out-of-scope situation by code, in file order."""
    path = path or OUT_OF_SCOPE_PATH
    return dict(_load(path, path.stat().st_mtime_ns))


def out_of_scope_codes(path: Path | None = None) -> tuple[str, ...]:
    return tuple(load_out_of_scope(path))


class _Blanks(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return f"[{key.replace('_', ' ')}]"


def out_of_scope(code: str, path: Path | None = None, **values: object) -> str:
    """The sentence for `code`, with `{blanks}` filled from `values` (an unfilled one reads `[like this]`).

    Raises `KeyError` for a code that is not in the file.
    """
    return load_out_of_scope(path)[code].say.format_map(_Blanks({k: str(v) for k, v in values.items()}))


def out_of_scope_request(code: str, path: Path | None = None) -> str | None:
    """The selector of the data request message to offer with `code`'s sentence, or None."""
    return load_out_of_scope(path)[code].request
