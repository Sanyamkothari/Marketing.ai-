"""The one place the Data Doctor's limits are read (`data_doctor.limits` in `configs/engine.yaml`).

Every number the Doctor's AI or a gate obeys, and that a prompt may state, lives in
:class:`engine.doctor.config.DataDoctorLimits`. A module asks :func:`limits` at the point of use instead of keeping
its own copy; the prompts are rendered with :func:`prompt_limits`, so a number the model is told is the number the
code enforces. The defaults equal the values these modules used to hard-code.

A case's own configuration wins for `turn` and `asks` when given (`limits(config)`); `gates` and `trial` are
engine-wide, whoever asks. Otherwise the engine defaults apply. The hard floors
(minimum rows and positives, leakage, consent) are not here and cannot be loosened through here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from engine.doctor.config import DataDoctorConfig, DataDoctorLimits

__all__ = ["limits", "prompt_limits", "use_limits"]

_ACTIVE: list[DataDoctorLimits] = []
"""A test's stand-in (see :func:`use_limits`); empty in production."""
_LOADED: dict[str, DataDoctorLimits] = {}


def limits(config: Any = None) -> DataDoctorLimits:
    """The limits in force: the `turn` and `asks` of `config` (a use case's configuration, or a `DataDoctorConfig`)
    when given, else the engine defaults from `configs/engine.yaml`; `gates` and `trial` are always the engine's.
    """
    if _ACTIVE:
        return _ACTIVE[-1]
    section = config if isinstance(config, DataDoctorConfig) else getattr(config, "data_doctor", None)
    found = _defaults()
    if isinstance(section, DataDoctorConfig):
        # What a prompt says (turn, asks) follows the case; the gates and the trials are engine-wide, because they
        # are enforced where no case configuration is at hand. One source, so a prompt never states another number.
        return found.model_copy(update={"turn": section.limits.turn, "asks": section.limits.asks})
    return found


def _defaults() -> DataDoctorLimits:
    from engine.config import config_root, load_engine_config

    key = str(config_root(None))
    found = _LOADED.get(key)
    if found is None:
        document = load_engine_config().defaults.get("data_doctor") or {}
        found = DataDoctorConfig.model_validate(document).limits
        _LOADED[key] = found
    return found


@contextmanager
def use_limits(replacement: DataDoctorLimits) -> Iterator[DataDoctorLimits]:
    """Run with `replacement` as the limits in force (tests)."""
    _ACTIVE.append(replacement)
    try:
        yield replacement
    finally:
        _ACTIVE.pop()


def prompt_limits(config: Any = None) -> dict[str, int | float]:
    """The limits a prompt may quote, flat, for its `{{ doctor_limits.<name> }}` variables."""
    found = limits(config)
    return {
        "retries": found.gates.retries,
        "strikes": found.turn.strikes,
        "blocking": found.asks.blocking,
        "quick": found.asks.quick,
        "quick_options_min": found.asks.quick_options_min,
        "quick_options_max": found.asks.quick_options_max,
        "chat_reply_chars": found.turn.chat_reply_chars,
        "guided_reply_chars": found.turn.guided_reply_chars,
        "summary_chars": found.turn.summary_chars,
        "request_columns": found.asks.request_columns,
        "doubts": found.asks.doubts,
        "requests": found.asks.requests,
    }
