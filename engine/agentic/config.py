"""Plan K's two configuration files: the switch and the policy (design §2.1, §11).

* `configs/agentic/agentic.yaml` - Plan K's three switches, each default **false**: `agentic.enabled` (M132),
  `compliance.gate.enabled` (M134) and `mcp.enabled` (M133); the last two are declared by the set-up (M130,
  DEC-1400 (d)) and read by nothing yet. With `agentic.enabled` off nothing of Plan K's
  agent-safe core is reachable: `api.main` imports only this module and does not mount the `/agentic`
  routes, the app creates no table and writes no file, and :func:`require_enabled` refuses the in-process
  entry points with `AGENTIC_DISABLED`. (`alembic upgrade head` still adds the empty `agentic_decision_log`
  table, revision 0009: a migration runs on every deployment, whatever the switch says.) A missing file is
  the default (off), so a configuration root from before Plan K behaves as before.
* `configs/agentic/policy.yaml` - the policy's rule sets and thresholds. :func:`policy_version` is the
  sha256 of the file's bytes; it is written into every ledger entry (`engine.agentic.ledger`).

Both are read from the configuration root (`engine.config.config_root`), like every other `configs/` file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from engine.agentic.canonical import sha256_hex
from engine.agentic.errors import AgenticError

__all__ = [
    "AGENTIC_CONFIG_FILE",
    "KNOWN_RULES",
    "POLICY_FILE",
    "AgenticConfig",
    "PolicyConfig",
    "agentic_enabled",
    "load_agentic_config",
    "load_policy",
    "policy_version",
    "require_enabled",
]

AGENTIC_CONFIG_FILE: Final[str] = "agentic/agentic.yaml"
POLICY_FILE: Final[str] = "agentic/policy.yaml"

KNOWN_RULES: Final[tuple[str, ...]] = (
    "holdout.immutable",
    "budget.cap",
    "consent.channel",
    "frequency.cap",
    "test.registered",
    "role.separation",
)
"""Every rule `engine.agentic.policy` implements (design §2.2). A rule set naming another is refused."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgenticSwitch(_Strict):
    enabled: bool = Field(default=False, description="The agent-safe core: tools, proposals, ledger, routes.")


class ComplianceGateSwitch(_Strict):
    enabled: bool = Field(
        default=False,
        description=(
            "The pre-send compliance gate (M134). Off until the integration plan's hand-off exists; nothing reads "
            "it yet (DEC-1400 (d))."
        ),
    )


class ComplianceSwitch(_Strict):
    gate: ComplianceGateSwitch = Field(default_factory=ComplianceGateSwitch)


class McpSwitch(_Strict):
    enabled: bool = Field(
        default=False,
        description="The verifier over MCP (M133): other agents' tool calls. Nothing reads it yet (DEC-1400 (d)).",
    )


class AgenticConfig(_Strict):
    """`configs/agentic/agentic.yaml`: Plan K's three switches (design §11), every one off by default.

    `agentic.enabled` (M132) gates the agent-safe core; `compliance.gate.enabled` (M134) and `mcp.enabled` (M133)
    are declared by the set-up (M130, DEC-1400 (d)) so that each later milestone reads a switch that already
    exists, documented and pinned off by `tests/unit/agentic/test_flags.py`.
    """

    agentic: AgenticSwitch = Field(default_factory=AgenticSwitch)
    compliance: ComplianceSwitch = Field(default_factory=ComplianceSwitch)
    mcp: McpSwitch = Field(default_factory=McpSwitch)


class HoldoutThresholds(_Strict):
    tolerance: Annotated[float, Field(ge=0.0, le=0.05)] = 0.005


class BudgetThresholds(_Strict):
    refuse_unpriced_when_capped: bool = True


class ConsentThresholds(_Strict):
    max_not_contactable: Annotated[int, Field(ge=0)] = 0
    require_recorded: bool = True


class ProposalSettings(_Strict):
    expire_after_days: Annotated[int, Field(ge=1, le=365)] = 14


class PolicyConfig(_Strict):
    """`configs/agentic/policy.yaml`: which rules apply to which proposal kind, and their thresholds."""

    schema_version: Literal[1] = 1
    rule_sets: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    holdout: HoldoutThresholds = Field(default_factory=HoldoutThresholds)
    budget: BudgetThresholds = Field(default_factory=BudgetThresholds)
    consent: ConsentThresholds = Field(default_factory=ConsentThresholds)
    proposals: ProposalSettings = Field(default_factory=ProposalSettings)

    @field_validator("rule_sets")
    @classmethod
    def _known_rules(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for kind, rules in value.items():
            unknown = sorted(set(rules) - set(KNOWN_RULES))
            if unknown:
                raise ValueError(f"rule set {kind!r} names unknown rules: {', '.join(unknown)}.")
            if len(set(rules)) != len(rules):
                raise ValueError(f"rule set {kind!r} names a rule twice.")
        return value


def _root(config_root: Path | None) -> Path:
    from engine.config import config_root as resolve_root

    return resolve_root(config_root)


def _read(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AgenticError("CONFIG_INVALID", f"{path.name} could not be read: {exc}.", status=500) from exc


def load_agentic_config(config_root: Path | None = None) -> AgenticConfig:
    """`agentic.yaml` under the root; the defaults (everything off) when the file is not there."""
    path = _root(config_root) / AGENTIC_CONFIG_FILE
    if not path.is_file():
        return AgenticConfig()
    try:
        return AgenticConfig.model_validate(_read(path))
    except ValidationError as exc:
        raise AgenticError(
            "CONFIG_INVALID", f"{AGENTIC_CONFIG_FILE} is not valid: {exc}.", status=500
        ) from exc


def agentic_enabled(config_root: Path | None = None) -> bool:
    """Whether `agentic.enabled` is on under this configuration root."""
    return load_agentic_config(config_root).agentic.enabled


def require_enabled(config_root: Path | None = None) -> None:
    """Refuse with `AGENTIC_DISABLED` (404) unless `agentic.enabled` is on."""
    if not agentic_enabled(config_root):
        raise AgenticError(
            "AGENTIC_DISABLED",
            "Agent proposals are switched off for this deployment (agentic.enabled is false).",
            status=404,
        )


def policy_version(config_root: Path | None = None) -> str:
    """The sha256 of `policy.yaml`'s bytes: the version every ledger entry records."""
    path = _root(config_root) / POLICY_FILE
    try:
        return sha256_hex(path.read_bytes())
    except OSError as exc:
        raise AgenticError("CONFIG_INVALID", f"{POLICY_FILE} could not be read.", status=500) from exc


def load_policy(config_root: Path | None = None) -> PolicyConfig:
    """`policy.yaml` under the root. A missing file is refused: there is no default policy to fall back on."""
    path = _root(config_root) / POLICY_FILE
    if not path.is_file():
        raise AgenticError(
            "CONFIG_INVALID", f"{POLICY_FILE} is missing; agent proposals need a policy.", status=500
        )
    try:
        return PolicyConfig.model_validate(_read(path))
    except ValidationError as exc:
        raise AgenticError("CONFIG_INVALID", f"{POLICY_FILE} is not valid: {exc}.", status=500) from exc
