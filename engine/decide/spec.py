"""Plan J M99's configuration types and the one channel-name rule (DEC-1309).

This module imports nothing from `engine`: `engine.config.SuppressionConfig` declares
`channels: ChannelMap` in place (Plan J §4.3, in-place declarations), so `engine.config` imports this
module at the top, as it imports `engine.holdout.spec` (M92), and a cycle would break both. The model
settings repeat `engine.config._Base`'s - frozen, unknown keys refused - and a bad channel name raises
`ValueError`, which the config loader reports as `CONFIG_INVALID` with the dotted path.

**One channel-name rule.** :data:`CHANNEL_NAME` is the pattern every channel name meets wherever it is
written: `actions.suppression.channels`, the catalogue's `channels` (`engine.decide.catalogue`) and a
consent record's `channel` (`engine.privacy.consent`, the import refuses a row whose channel does not
match). Names are compared lower case, so `SMS` in a ledger file is `sms` everywhere.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict

__all__ = [
    "CHANNEL_NAME",
    "CHANNEL_NAME_RULE",
    "ChannelMap",
    "ChannelSuppressionConfig",
    "channel_names",
]

CHANNEL_NAME: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,39}")
"""A channel name, after stripping and lower-casing: a letter, then up to 39 letters, digits or `_`."""

CHANNEL_NAME_RULE: Final[str] = "lower case letters, digits and _"
"""How :data:`CHANNEL_NAME` is described in a message."""

_SETTINGS: Final[ConfigDict] = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    use_enum_values=False,
    str_strip_whitespace=True,
    populate_by_name=True,
    protected_namespaces=(),
)
"""`engine.config._Base`'s settings, repeated: this module cannot import it."""


class ChannelSuppressionConfig(BaseModel):
    """One channel's consent and contactability columns (Plan J M99, DEC-1309).

    Either column may be null; a configured column the scoring file lacks is skipped with a warning,
    like a Phase 1 suppression column (DEC-030). Truthiness is Phase 1's (`engine.stages.actions`, read
    through `engine.decide.contactability.truthy`): a null is not a consent.
    """

    model_config = _SETTINGS

    consent_column: str | None = None
    contactable_column: str | None = None


def channel_names(value: Any) -> Any:
    """`actions.suppression.channels`' keys, stripped and lower case; a bad or repeated name is refused."""
    if not isinstance(value, Mapping):
        return value
    names = [str(name).strip().lower() for name in value]
    for name in names:
        if not CHANNEL_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a channel name ({CHANNEL_NAME_RULE})")
    if len(set(names)) != len(names):
        raise ValueError("a channel is listed twice")
    return dict(zip(names, value.values(), strict=True))


ChannelMap = Annotated[dict[str, ChannelSuppressionConfig], BeforeValidator(channel_names)]
"""`actions.suppression.channels`: per channel, in order of preference, its consent columns."""
