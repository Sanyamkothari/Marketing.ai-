"""The gate's switch and settings, and the URL whitelist it reads (Plan K M134; design §11).

* The switch is `compliance.gate.enabled` in `configs/agentic/agentic.yaml`, one of Plan K's three switches
  (M130, DEC-1400 (d); read through `engine.agentic.config`), default **false**; a missing file is off, so a
  configuration root from before Plan K behaves as before. It is the only switch (DEC-1405 (a)): the builder had
  its own `enabled` here, because M130 was not on its branch, and at integration it was moved, as `mcp.enabled`
  was (DEC-1404 (a)). With it off a treat list build reads the two files and nothing else of `engine.compliance`,
  and every treat list is byte for byte what it was.
* `configs/compliance/compliance.yaml` - the gate's settings only: `opt_in_channels` (default `[whatsapp]`: the
  channels sent on only with a grant on that channel itself in the consent ledger) and `dlt_var_max_chars`
  (default 30; null for no limit). A missing file is the defaults. A switch written here
  (`compliance.gate.enabled`) is refused with `COMPLIANCE_SWITCH_MISPLACED`, never ignored, so nobody believes
  they turned the gate on (or off) in a file that does not decide it.
* `configs/compliance/url_whitelist.yaml` (`engine.compliance.urls.UrlWhitelist`), absent by default; else a
  `url_whitelist` block of the catalogue's region file. With neither, the URL check is not applied.

Read from the configuration root (`engine.config.config_root`), like every other `configs/` file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from engine.compliance.errors import ComplianceError
from engine.compliance.urls import UrlWhitelist
from engine.decide.spec import CHANNEL_NAME, CHANNEL_NAME_RULE

__all__ = [
    "COMPLIANCE_CONFIG_FILE",
    "SWITCH_MISPLACED_CODE",
    "URL_WHITELIST_FILE",
    "ComplianceConfig",
    "GateSettings",
    "gate_enabled",
    "load_compliance_config",
    "load_url_whitelist",
]

COMPLIANCE_CONFIG_FILE: Final[str] = "compliance/compliance.yaml"
URL_WHITELIST_FILE: Final[str] = "compliance/url_whitelist.yaml"
REGION_WHITELIST_KEY: Final[str] = "url_whitelist"
SWITCH_MISPLACED_CODE: Final[str] = "COMPLIANCE_SWITCH_MISPLACED"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GateSettings(_Strict):
    """The gate's settings. Its switch is not here: `compliance.gate.enabled` is in `configs/agentic/agentic.yaml`."""

    opt_in_channels: tuple[str, ...] = Field(
        default=("whatsapp",),
        description=(
            "Channels a customer is sent on only when the consent ledger holds a valid grant on that channel "
            "itself (the opt-in record); an all-channel grant does not count."
        ),
    )
    dlt_var_max_chars: Annotated[int, Field(ge=1, le=500)] | None = Field(
        default=30,
        description="The longest value of a {#var#}, {#numeric#} or {#alphanumeric#} slot; null for no limit.",
    )

    @field_validator("opt_in_channels", mode="before")
    @classmethod
    def _channel_names(cls, value: object) -> object:
        if not isinstance(value, list | tuple):
            return value
        names = tuple(str(item).strip().lower() for item in value)
        for name in names:
            if not CHANNEL_NAME.fullmatch(name):
                raise ValueError(f"{name!r} is not a channel name ({CHANNEL_NAME_RULE})")
        return tuple(dict.fromkeys(names))


class _Compliance(_Strict):
    gate: GateSettings = Field(default_factory=GateSettings)


class ComplianceConfig(_Strict):
    """`configs/compliance/compliance.yaml`: the gate's settings (its switch is in `configs/agentic/agentic.yaml`)."""

    compliance: _Compliance = Field(default_factory=_Compliance)

    @property
    def gate(self) -> GateSettings:
        return self.compliance.gate


def _root(config_root: Path | None) -> Path:
    from engine.config import config_root as resolve_root

    return resolve_root(config_root)


def _read(path: Path) -> Any:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ComplianceError("CONFIG_INVALID", f"{path.name} could not be read: {exc}.") from exc


def _refuse_switch(document: Any) -> None:
    """`COMPLIANCE_SWITCH_MISPLACED` when the file sets `compliance.gate.enabled` (DEC-1405 (a))."""
    compliance = document.get("compliance") if isinstance(document, dict) else None
    gate = compliance.get("gate") if isinstance(compliance, dict) else None
    if isinstance(gate, dict) and "enabled" in gate:
        raise ComplianceError(
            SWITCH_MISPLACED_CODE,
            f"{COMPLIANCE_CONFIG_FILE} sets compliance.gate.enabled, but the gate's switch is in "
            "agentic/agentic.yaml (compliance: gate: enabled). Remove it here and set it there; this file "
            "holds the gate's settings only.",
        )


def load_compliance_config(config_root: Path | None = None) -> ComplianceConfig:
    """`compliance.yaml` under the root; the defaults when the file is not there. A switch in it is refused."""
    path = _root(config_root) / COMPLIANCE_CONFIG_FILE
    if not path.is_file():
        return ComplianceConfig()
    document = _read(path)
    _refuse_switch(document)
    try:
        return ComplianceConfig.model_validate(document)
    except ValidationError as exc:
        raise ComplianceError("CONFIG_INVALID", f"{COMPLIANCE_CONFIG_FILE} is not valid: {exc}.") from exc


def gate_enabled(config_root: Path | None = None) -> bool:
    """Whether `compliance.gate.enabled` (`configs/agentic/agentic.yaml`) is on under this configuration root.

    `compliance.yaml` is validated as well, whatever the switch says, so a switch misplaced there or a settings
    file that is not valid is refused with its code rather than ignored. An invalid `agentic.yaml` is
    `CONFIG_INVALID`.
    """
    from engine.agentic.config import load_agentic_config
    from engine.agentic.errors import AgenticError

    try:
        enabled = load_agentic_config(config_root).compliance.gate.enabled
    except AgenticError as exc:
        raise ComplianceError(exc.code, exc.message) from exc
    load_compliance_config(config_root)
    return enabled


def load_url_whitelist(
    config_root: Path | None = None, region_data: dict[str, Any] | None = None
) -> tuple[UrlWhitelist | None, str | None]:
    """The whitelist and where it came from: `url_whitelist.yaml`, else the region file's block, else
    `(None, None)` - the URL check is then not applied."""
    path = _root(config_root) / URL_WHITELIST_FILE
    source: str | None = None
    document: Any = None
    if path.is_file():
        document, source = _read(path), URL_WHITELIST_FILE
    elif region_data is not None and region_data.get(REGION_WHITELIST_KEY) is not None:
        document, source = region_data[REGION_WHITELIST_KEY], f"region file: {REGION_WHITELIST_KEY}"
    if source is None:
        return None, None
    try:
        return UrlWhitelist.model_validate(document), source
    except ValidationError as exc:
        raise ComplianceError("CONFIG_INVALID", f"The URL whitelist ({source}) is not valid: {exc}.") from exc
