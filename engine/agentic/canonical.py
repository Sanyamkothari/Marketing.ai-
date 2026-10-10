"""The one canonical JSON every Plan K hash is taken over (design §3.2).

Two agents that ask the same question of the same artefacts must get the same evidence id, ten runs of a
scenario must give the same payload digest (pass^k), and a ledger entry must hash the same on every
machine. All three need one byte representation of a value, so there is exactly one:

* keys sorted, no insignificant whitespace (`,` and `:` separators);
* UTF-8, with non-ASCII characters written as themselves (not `\\u` escapes);
* floats as Python's shortest round-trip `repr` (`0.1`, not `0.1000000000000000055`); NaN and infinity are
  refused, because JSON has no spelling for them and two writers would disagree;
* tuples are lists; dates and datetimes are ISO 8601; a pydantic model is its JSON-mode dump. Anything else
  (a set, bytes, a Decimal) is refused rather than guessed at, because its order or spelling is not defined.

Every hash in `engine.agentic` goes through :func:`digest` or :func:`sha256_hex` over :func:`canonical_bytes`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

__all__ = ["canonical_bytes", "canonical_json", "digest", "jsonable", "sha256_hex"]


def jsonable(value: Any) -> Any:
    """`value` as plain JSON types (dict with str keys, list, str, int, float, bool, None)."""
    if isinstance(value, BaseModel):
        return jsonable(value.model_dump(mode="json"))
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Canonical JSON has no spelling for NaN or infinity.")
        return value
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"Canonical JSON keys must be text, not {type(key).__name__}.")
            out[key] = jsonable(item)
        return out
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    raise TypeError(f"{type(value).__name__} has no canonical JSON form.")


def canonical_json(value: Any) -> str:
    """The canonical JSON text of `value` (see the module docstring)."""
    return json.dumps(
        jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_bytes(value: Any) -> bytes:
    """:func:`canonical_json` encoded as UTF-8: what every hash is taken over."""
    return canonical_json(value).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    """The SHA-256 of `data` (text is UTF-8 encoded first), as 64 lower-case hex characters."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def digest(value: Any) -> str:
    """`sha256_hex(canonical_bytes(value))`: the content hash of any value."""
    return sha256_hex(canonical_bytes(value))
