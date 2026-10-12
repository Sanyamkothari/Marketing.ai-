"""The `data_doctor:` block of a use case (Plan O): the switch that turns the one Data Doctor agent on.

Plan O (`docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md`) builds one agent, Data Doctor, whose tools are every data
capability the product has. It is built behind one switch, `data_doctor.unified`, which stays off until the plan's
parity checklist (§3.2) is met at M249 (decision O14). While it is off, nothing the product does changes: every
existing test and golden digest stays byte-identical. Defaults live in `configs/engine.yaml` under
`defaults.data_doctor`; a use-case file overrides only what differs.

This module imports nothing from `engine`, for the reason `engine/agent/config.py` gives: `engine.config.UseCaseConfig`
declares a field of type :class:`DataDoctorConfig`, so `engine.config` imports this module at the top and a cycle
would break both. The settings repeat `engine.config._Base` (frozen, unknown keys refused), and a malformed value
raises `ValueError`, which the config loader reports as `CONFIG_INVALID` with the dotted path.

The block is **not overridable per run**: none of its paths is in `overridable_paths()`, so a run override that
names one is refused with `OVERRIDE_UNKNOWN_PATH`. It is outside `Recipe.recipe_hash`, and `UseCaseConfig` leaves
it out of a dump while it holds only defaults, so a run's `run_config.json` is unchanged while the switch is off
(DEC-1800 (c)).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool

__all__ = ["DataDoctorConfig"]

_SETTINGS: Final[ConfigDict] = ConfigDict(
    extra="forbid",
    frozen=True,
    validate_default=True,
    use_enum_values=False,
    str_strip_whitespace=True,
    populate_by_name=True,
    protected_namespaces=(),
)


class DataDoctorConfig(BaseModel):
    """`data_doctor:` in a use case: Plan O's one Data Doctor agent: the switch (M230) and the workspace currency (M242)."""

    model_config = _SETTINGS

    unified: StrictBool = Field(
        default=False,
        description=(
            "Plan O (DEC-1800): when true, Guided setup and the Doctor page are the one Data Doctor agent. Off "
            "until the parity checklist is met (M249); while off, nothing changes."
        ),
    )
    workspace_currency: str = Field(
        default="INR",
        pattern=r"^[A-Z]{3}$",
        exclude_if=lambda value: value == "INR",
        description=(
            "Plan O M242 (DEC-1812): the currency this workspace's reports use, as a code (INR, USD, EUR). A client's "
            "confirmed locale profile overrides it for that client. Read only while `unified` is on; INR, the "
            "default, is left out of a dump so nothing moves."
        ),
    )
