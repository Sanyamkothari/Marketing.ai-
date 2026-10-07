"""The holdout's configuration types and the one helper every reader of the holdout fraction uses.

This module imports nothing from `engine`: `engine.config.ActionsConfig` declares
`holdout: HoldoutConfig` and `explore_fraction: ExploreFraction` in place (Plan J §3.2, in-place
declarations), so `engine.config` imports this module at the top and a cycle would break both. The
settings repeat `engine.config._Base`'s - frozen, unknown keys refused - and a cross-field failure
raises `ValueError`, which the config loader reports as `CONFIG_INVALID` with the dotted path, as
`engine.uplift.config` does.

**Two models, on purpose.** `HoldoutConfig` is what a use case *asks for* (`scope`, `fraction`).
`HoldoutSpec` is what a run *used*: the same two plus `salt_id` (the salt's fingerprint, from the
settings, never from a file) and `epoch` (from the platform database's ledger). A configuration
file therefore cannot claim an epoch or a salt it does not have.

**Which fraction.** `actions.control_group_fraction` keeps its meaning under `scope: run` (today's
per-run draw). Under a persistent scope `actions.holdout.fraction` is required and wins;
`control_group_fraction` is then not read by the draw. :func:`effective_holdout_fraction` is the one
place that decides, so no reader ever sees two numbers (DEC-1302 (d)).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "HOLDOUT_FRACTION_LOWERED",
    "HOLDOUT_FRACTION_MISMATCH",
    "HOLDOUT_REPORT_FILENAME",
    "HOLDOUT_SALT_CHANGED",
    "HOLDOUT_SALT_MISSING",
    "HOLDOUT_SALT_UNCHANGED",
    "HOLDOUT_SCOPES",
    "HOLDOUT_SCOPE_RESERVED",
    "MAX_EXPLORE_FRACTION",
    "MAX_HOLDOUT_FRACTION",
    "PERSISTENT_SCOPES",
    "RESERVED_SCOPE_KEYS",
    "UNIVERSAL_SCOPE_KEY",
    "ExploreFraction",
    "HoldoutAssignmentReport",
    "HoldoutConfig",
    "HoldoutError",
    "HoldoutLedgerEntry",
    "HoldoutScope",
    "HoldoutSpec",
    "PersistentScope",
    "effective_holdout_fraction",
    "is_persistent",
    "ledger_key",
    "reserved_scope_error",
    "scope_key",
    "service_on",
]

PersistentScope = Literal["use_case", "universal"]
HoldoutScope = Literal["run", "use_case", "universal"]
"""`run`: today's per-run draw (the default). `use_case`: one holdout per use case. `universal`: one
holdout across every use case that uses this scope."""

HOLDOUT_SCOPES: Final[tuple[str, ...]] = ("run", "use_case", "universal")
PERSISTENT_SCOPES: Final[frozenset[str]] = frozenset({"use_case", "universal"})

MAX_HOLDOUT_FRACTION: Final[float] = 0.50
"""The same upper bound as `actions.control_group_fraction`."""

MAX_EXPLORE_FRACTION: Final[float] = 0.10
"""`actions.explore_fraction`'s upper bound (Plan J J4: 0 to 10%)."""

UNIVERSAL_SCOPE_KEY: Final[str] = "universal"

RESERVED_SCOPE_KEYS: Final[frozenset[str]] = frozenset({UNIVERSAL_SCOPE_KEY, "explore"})
"""Use-case ids that cannot take `scope: use_case`: their membership text would be the universal
holdout's (`{salt}:universal:{entity}`) or share the explore draw's prefix (`{salt}:explore:...`)."""

# Error codes (Plan J §5.7 style; help text in `configs/pilot/help.yaml`, added by the integrator).
HOLDOUT_SALT_MISSING: Final[str] = "HOLDOUT_SALT_MISSING"
HOLDOUT_SALT_CHANGED: Final[str] = "HOLDOUT_SALT_CHANGED"
HOLDOUT_SALT_UNCHANGED: Final[str] = "HOLDOUT_SALT_UNCHANGED"
HOLDOUT_FRACTION_LOWERED: Final[str] = "HOLDOUT_FRACTION_LOWERED"
HOLDOUT_FRACTION_MISMATCH: Final[str] = "HOLDOUT_FRACTION_MISMATCH"
HOLDOUT_SCOPE_RESERVED: Final[str] = "HOLDOUT_SCOPE_RESERVED"

ExploreFraction = Annotated[float, Field(ge=0.0, le=MAX_EXPLORE_FRACTION)]
"""`actions.explore_fraction`: the share of eligible customers outside the target treated anyway."""

_SETTINGS: Final[ConfigDict] = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    str_strip_whitespace=True,
    populate_by_name=True,
    protected_namespaces=(),
)


class HoldoutError(Exception):
    """A holdout that cannot be used as configured. `code` and `message` reach the run's error as they are."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HoldoutConfig(BaseModel):
    """`actions.holdout`: which holdout a use case asks for. Not overridable per run."""

    model_config = _SETTINGS

    scope: HoldoutScope = Field(
        default="run",
        description=(
            "run: a new control group every run (actions.control_group_fraction). use_case: the same "
            "customers held out every run of this use case. universal: the same customers held out of "
            "every use case that uses this scope."
        ),
    )
    fraction: Annotated[float, Field(gt=0.0, le=MAX_HOLDOUT_FRACTION)] | None = Field(
        default=None,
        description="Share of all customers held out under a persistent scope; required there, unused under run.",
    )

    @model_validator(mode="after")
    def _fraction_matches_scope(self) -> Self:
        if self.scope == "run" and self.fraction is not None:
            raise ValueError(
                "actions.holdout.fraction applies only to a persistent scope (use_case or universal); "
                "under scope run the control group is actions.control_group_fraction"
            )
        if self.scope != "run" and self.fraction is None:
            raise ValueError(
                f"actions.holdout.fraction is required when actions.holdout.scope is {self.scope}: a "
                "persistent holdout must say what share of customers it holds out"
            )
        return self


class HoldoutSpec(BaseModel):
    """What one run's holdout was: the scope, the fraction, the salt's fingerprint and the epoch."""

    model_config = _SETTINGS

    scope: HoldoutScope = Field(description="run, use_case or universal.")
    fraction: float = Field(ge=0.0, le=MAX_HOLDOUT_FRACTION, description="The effective holdout fraction.")
    salt_id: str | None = Field(
        default=None,
        description="First 16 hex characters of the holdout salt's fingerprint; null under scope run.",
    )
    epoch: int | None = Field(
        default=None, ge=1, description="The ledger epoch the run belongs to; null under scope run."
    )
    scope_key: str | None = Field(
        default=None, description="universal, or the use-case id under scope use_case; null under scope run."
    )
    explore_fraction: float = Field(
        default=0.0, ge=0.0, le=MAX_EXPLORE_FRACTION, description="actions.explore_fraction of the run."
    )


class HoldoutLedgerEntry(BaseModel):
    """One persistent holdout's current epoch, as `platform_setting` keeps it (never a secret, never a person)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: PersistentScope = Field(description="The persistent scope.")
    scope_key: str = Field(description="universal, or the use-case id.")
    epoch: int = Field(ge=1, description="Starts at 1; a lower fraction or a new salt starts the next one.")
    fraction: float = Field(
        gt=0.0, le=MAX_HOLDOUT_FRACTION, description="The largest fraction used in this epoch so far."
    )
    salt_id: str = Field(description="The salt fingerprint (first 16 hex characters) of this epoch.")
    started_at: datetime = Field(description="When this epoch started.")
    updated_at: datetime = Field(description="When this entry last changed (a raised fraction, say).")


HOLDOUT_REPORT_FILENAME: Final[str] = "holdout_assignment.json"
"""`runs/<run_id>/holdout_assignment.json`: the run's `HoldoutSpec` and counts (aggregate, no keys)."""


class HoldoutAssignmentReport(BaseModel):
    """`holdout_assignment.json`: which holdout a scoring run used, and how many rows it marked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1, description="Bumped only on a breaking change.")
    run_id: str = Field(description="The scoring run.")
    use_case_id: str = Field(description="Its use case.")
    spec: HoldoutSpec = Field(description="The holdout the run used.")
    rows: int = Field(ge=0, description="Rows in holdout_assignment.parquet (every scored row).")
    holdout_members: int = Field(
        ge=0, description="Rows whose customer is a holdout member, suppressed or not."
    )
    control_rows: int = Field(ge=0, description="Members that were eligible: the run's control group.")
    explore_candidates: int = Field(
        ge=0, description="Eligible rows outside the holdout, not selected and not a predicted sleeping dog."
    )
    explore_rows: int = Field(ge=0, description="Candidates drawn into the explore slice.")
    created_at: datetime = Field(description="When the file was written.")


class _Actions(Protocol):
    """The two fields of `engine.config.ActionsConfig` the helper reads (this module cannot import it)."""

    @property
    def control_group_fraction(self) -> float: ...

    @property
    def holdout(self) -> HoldoutConfig: ...


def is_persistent(actions: _Actions) -> bool:
    """True under `use_case` or `universal`."""
    return actions.holdout.scope in PERSISTENT_SCOPES


def effective_holdout_fraction(actions: _Actions) -> float:
    """The holdout share a run of this configuration holds back (DEC-1302 (d)).

    `actions.control_group_fraction` under `scope: run`; `actions.holdout.fraction` under a
    persistent scope (the validator guarantees it is set there).
    """
    if is_persistent(actions) and actions.holdout.fraction is not None:
        return float(actions.holdout.fraction)
    return float(actions.control_group_fraction)


def service_on(actions: _Actions, explore_fraction: float) -> bool:
    """Whether a scoring run engages the holdout service: a persistent scope or an explore slice.

    Off (the default) the run is today's, byte for byte, and writes neither holdout file (DEC-1302
    (e)); `engine.holdout.assign.assignment_frame` derives the same table from its scores on demand.
    """
    return is_persistent(actions) or explore_fraction > 0.0


def scope_key(scope: str, use_case_id: str) -> str | None:
    """The `scope_key` of the membership hash: `universal`, the use-case id, or None under `run`."""
    if scope == "universal":
        return UNIVERSAL_SCOPE_KEY
    if scope == "use_case":
        return use_case_id
    return None


def reserved_scope_error(scope: str, use_case_id: str) -> HoldoutError | None:
    """The refusal for `scope: use_case` on a reserved use-case id (`RESERVED_SCOPE_KEYS`), else None."""
    if scope != "use_case" or use_case_id not in RESERVED_SCOPE_KEYS:
        return None
    return HoldoutError(
        HOLDOUT_SCOPE_RESERVED,
        f"A use case named {use_case_id!r} cannot have its own holdout (actions.holdout.scope: use_case): "
        f"the name is reserved by the holdout itself. Rename the use case, or use scope universal or run.",
    )


def ledger_key(scope: str, key: str) -> str:
    """The `platform_setting` key of one persistent holdout's ledger entry."""
    return f"holdout_epoch:{scope}:{key}"
