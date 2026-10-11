"""Links in a message, and the client's URL whitelist (Plan K M134, DEC-1405 (d)).

Every link in an approved message must be on the client's whitelist (an Indian operator delivers a
promotional SMS only with whitelisted links). The list is data, read from `configs/compliance/url_whitelist.yaml`
or, when that file is absent, from a `url_whitelist` block of the catalogue's region file; with neither, the
URL check is **not applied**, and the gate's `rule_version` records that (`url_whitelist: null`).

```yaml
channels: [sms]                       # the channels whose messages must use listed links; omit for every channel
urls:
  - shop.example.in                   # a host: any link to exactly this host
  - "*.example.in"                    # any sub-domain of example.in (not example.in itself)
  - https://example.in/offers/        # a prefix: links that start with it (same scheme, host and path start)
```

**Finding links.** A link starts with `http://`, `https://` or `www.` (any case) and runs to the next white
space; closing punctuation at its end (`.`, `,`, `)` and the like) is not part of it. A link written without a
scheme (`www.example.in/x`) matches host entries only, never a prefix that names a scheme.

**Matching a prefix.** Same scheme, same host and port (case-insensitive), and the link's path starts with the
entry's path at a boundary: `https://example.in/offers` lists `/offers` and `/offers/diwali`, not `/offers2`.
Linear in the message length: one regular expression scan with no nested repetition.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.decide.spec import CHANNEL_NAME, CHANNEL_NAME_RULE

__all__ = ["UrlWhitelist", "find_urls"]

_LINK: Final[re.Pattern[str]] = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"']+")
_TRAILING: Final[str] = ".,;:!?)]}'\""
_HOST: Final[re.Pattern[str]] = re.compile(r"(?:\*\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?")


def find_urls(text: str) -> tuple[str, ...]:
    """Every link in `text`, in order, without closing punctuation."""
    return tuple(found.group(0).rstrip(_TRAILING) for found in _LINK.finditer(text))


def _split(url: str) -> SplitResult | None:
    """`url` split into parts; a scheme-less `www.` link is read with no scheme. None when it does not parse."""
    try:
        if url.lower().startswith("www."):
            return urlsplit("//" + url)
        return urlsplit(url)
    except ValueError:
        return None


class UrlWhitelist(BaseModel):
    """The links a client may send (see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    channels: tuple[str, ...] | None = Field(
        default=None,
        description="Channels whose messages must use listed links; null (the default) means every channel.",
    )
    urls: tuple[str, ...] = Field(min_length=1, description="Hosts, `*.` sub-domain hosts, or URL prefixes.")

    @field_validator("channels", mode="before")
    @classmethod
    def _channel_names(cls, value: object) -> object:
        if not isinstance(value, list | tuple):
            return value
        names = tuple(str(item).strip().lower() for item in value)
        for name in names:
            if not CHANNEL_NAME.fullmatch(name):
                raise ValueError(f"{name!r} is not a channel name ({CHANNEL_NAME_RULE})")
        return tuple(dict.fromkeys(names))

    @field_validator("urls")
    @classmethod
    def _entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for entry in value:
            if "://" in entry:
                parts = _split(entry)
                if parts is None or not parts.scheme or not parts.netloc:
                    raise ValueError(f"{entry!r} is not a URL prefix (scheme://host/path).")
            elif not _HOST.fullmatch(entry.lower()):
                raise ValueError(f"{entry!r} is neither a host (such as shop.example.in) nor a URL prefix.")
        return value

    def applies_to(self, channel: str) -> bool:
        """Whether messages on `channel` must use listed links."""
        return self.channels is None or channel in self.channels

    def allows(self, url: str) -> bool:
        """Whether the link `url` is on the list."""
        link = _split(url)
        if link is None or not link.netloc:
            return False
        host = (link.hostname or "").lower()
        netloc = link.netloc.lower()
        for entry in self.urls:
            lowered = entry.lower()
            if "://" not in entry:
                if lowered.startswith("*."):
                    if host.endswith(lowered[1:]):
                        return True
                elif ":" in lowered:
                    if netloc == lowered:
                        return True
                elif host == lowered:
                    return True
                continue
            prefix = _split(entry)
            if prefix is None or not link.scheme or link.scheme.lower() != prefix.scheme.lower():
                continue
            if netloc != prefix.netloc.lower():
                continue
            if _path_starts(link, prefix):
                return True
        return False


def _path_starts(link: SplitResult, prefix: SplitResult) -> bool:
    path = link.path or "/"
    wanted = prefix.path or "/"
    if wanted.endswith("/"):
        return path.startswith(wanted) or path + "/" == wanted
    return path == wanted or path.startswith(wanted + "/")
