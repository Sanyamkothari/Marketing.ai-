"""The role catalogue in use: cross-field checks a use case's onboarding blocks must pass.

`engine/config.py` validates each Phase 2 block on its own - a feature that is a `mean` names the
column it averages, a label that looks forward names a horizon. What it deliberately does *not* do
is check those blocks against each other, or against `configs/roles.yaml`, because
`PARALLEL_WORK_PROTOCOL.md` §4 confines this branch to one marked block in that shared file and
those checks would have to live above it, in `UseCaseConfig._cross_field`.

They live here instead, which costs one thing and buys two. It costs the moment of failure: a use
case naming a role that does not exist fails when onboarding reads it rather than when the config
loader does, so `test_every_shipped_use_case_passes_the_onboarding_checks` exists to catch exactly
that in CI instead. It buys a shared file this branch can rebase without conflicts, and it keeps
`engine/config.py` from importing a role catalogue it has no other use for.

Nothing here branches on a use-case id or a client id; the checks read the config and the catalogue
and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.config import ConfigError, LabelType, RoleCatalogue, get_roles

if TYPE_CHECKING:
    from engine.config import UseCaseConfig

__all__ = [
    "check_label",
    "check_suggested_features",
    "validate_onboarding_config",
]


def validate_onboarding_config(config: UseCaseConfig, roles: RoleCatalogue | None = None) -> None:
    """Every cross-field rule the Phase 2 blocks of one use case must satisfy.

    Raises the first `ConfigError` it finds, with the dotted path of the offending key, exactly as
    the config loader does for the blocks it can check itself.
    """
    catalogue = get_roles() if roles is None else roles
    check_suggested_features(config, catalogue)
    check_label(config, catalogue)


def check_suggested_features(config: UseCaseConfig, roles: RoleCatalogue) -> None:
    """Names are unique, name nothing the dataset already means, and each one names a real role."""
    names = [feature.name for feature in config.suggested_features]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(
            "SUGGESTED_FEATURE_DUPLICATE",
            f"suggested_features lists {', '.join(duplicates)} more than once.",
            path="suggested_features",
        )
    schema = config.standard_schema
    collisions = sorted(set(names) & {*schema.column_names, *schema.reserved_names})
    if collisions:
        raise ConfigError(
            "FEATURE_NAME_COLLISION",
            f"{', '.join(collisions)} is both a standard column and a suggested feature; "
            "a built column can only mean one thing.",
            path="suggested_features",
        )
    for index, feature in enumerate(config.suggested_features):
        roles.require(feature.role, path=f"suggested_features[{index}].role")


def check_label(config: UseCaseConfig, roles: RoleCatalogue) -> None:
    """The label agrees with `target`, and a label derived from events names an event role."""
    label = config.label
    if label is None:
        return
    if label.type is LabelType.COLUMN:
        if config.target.column is not None and label.column != config.target.column:
            raise ConfigError(
                "LABEL_TARGET_MISMATCH",
                f"label.column ({label.column}) and target.column ({config.target.column}) name "
                "different columns, so the engine cannot tell which one is the outcome.",
                path="label.column",
            )
        return
    spec = roles.require(label.role or "", path="label.role")
    if not spec.is_event:
        raise ConfigError(
            "LABEL_ROLE_NOT_EVENT",
            f"A {label.type.value} label looks forward in time, so its role must be an event "
            f"table; {label.role!r} is an entity table.",
            path="label.role",
        )
