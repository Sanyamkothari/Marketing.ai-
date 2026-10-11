"""The model licence allow-list (Plan K M143, DEC-1410, proposal K8).

`configs/agentic/model_licences.yaml` says which AutoGluon presets and model keys the product may load.
Prior Labs' licences for TabPFN-2.5 and later bar commercial use (including using a model's outputs for
business decisions), so a strategy that would load one is refused when the configuration is resolved,
before anything is trained: `MODEL_LICENCE_BLOCKED`. The list is **default deny**: a preset or model key
nobody has reviewed is refused too, so a new library release cannot add a model to a client's run
unseen.

Two checks, both cheap enough to run on every configuration resolve:

* `ModelLicences.check_preset` - the preset the chosen strategy maps to (`catalog.strategy_presets`);
* `ModelLicences.check_model_key` - the AutoGluon keys of the algorithms the run selects.

What a preset *really* loads is read from the installed AutoGluon by `portfolio_model_keys`, which
imports it and so is not called while a configuration resolves; the test suite runs it for every
allowed preset (and the AutoGluon pin is exact), which is what keeps the name-level check honest.

The file is read from the config root being resolved; a root that has no copy of its own is read
against the shipped one, never against nothing.
"""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from engine.config import DEFAULT_CONFIG_ROOT, ConfigError, StrictBase, config_root, load_yaml

if TYPE_CHECKING:
    from engine.config import UseCaseConfig

__all__ = [
    "FOUNDATION_NOT_LOADED_WARNING",
    "LICENCES_RELATIVE_PATH",
    "MODEL_LICENCE_BLOCKED",
    "ModelLicences",
    "foundation_warnings",
    "load_model_licences",
    "portfolio_model_keys",
    "validate_model_licences",
]

MODEL_LICENCE_BLOCKED: Final[str] = "MODEL_LICENCE_BLOCKED"
LICENCES_RELATIVE_PATH: Final[Path] = Path("agentic") / "model_licences.yaml"
SCHEMA_VERSION: Final[int] = 1

FOUNDATION_NOT_LOADED_WARNING: Final[str] = (
    "model_search.strategy is foundation: the engine fits only the algorithms selected under "
    "model_search.candidates, so the pre-trained table models of this strategy (TabICLv2, TabDPT-Turbo) "
    "are not loaded yet; only the extreme_quality settings that do not depend on them apply"
)
"""Said on every resolve that picks `foundation`, while `engine/stages/train.py` passes its own
`hyperparameters` (which AutoGluon prefers over a preset's portfolio). It is true of the installed engine,
measured by a fit with `presets=extreme_quality` and `hyperparameters={"XGB": {}, "GBM": {}}`, which
trained exactly those two models and a weighted ensemble. Remove it when the train stage hands the
preset's own portfolio to AutoGluon for this strategy (docs/CROSS_BRANCH_REQUESTS.md)."""


class ModelLicences(StrictBase):
    """`configs/agentic/model_licences.yaml`."""

    schema_version: int
    allowed_presets: tuple[str, ...]
    blocked_presets: dict[str, str]
    allowed_model_keys: tuple[str, ...]
    blocked_model_keys: dict[str, str]

    def check_preset(self, preset: str, *, strategy: str | None = None) -> None:
        """Raise `MODEL_LICENCE_BLOCKED` unless `preset` is on the allow-list."""
        path = None if strategy is None else "model_search.strategy"
        if strategy is None:
            subject, joiner = f"The AutoGluon preset {preset!r}", " "
        else:
            subject, joiner = (
                f"The search strategy {strategy!r} uses the AutoGluon preset {preset!r},",
                " which ",
            )
        reason = self.blocked_presets.get(preset)
        if reason is not None:
            raise ConfigError(
                MODEL_LICENCE_BLOCKED,
                f"{subject}{joiner}{reason}. Models whose licence bars commercial use are never loaded.",
                path=path,
            )
        if preset not in self.allowed_presets:
            raise ConfigError(
                MODEL_LICENCE_BLOCKED,
                f"{subject}{joiner}is not on the allow-list of presets reviewed for commercial use "
                f"({LICENCES_RELATIVE_PATH.as_posix()}), so it is not loaded.",
                path=path,
            )

    def check_model_key(self, key: str, *, path: str | None = None) -> None:
        """Raise `MODEL_LICENCE_BLOCKED` unless the AutoGluon model key is on the allow-list."""
        upper = key.upper()
        for pattern, reason in self.blocked_model_keys.items():
            if fnmatch.fnmatchcase(upper, pattern.upper()):
                raise ConfigError(
                    MODEL_LICENCE_BLOCKED,
                    f"The model {key!r} is {reason}. Models whose licence bars commercial use are never loaded.",
                    path=path,
                )
        if upper not in {allowed.upper() for allowed in self.allowed_model_keys}:
            raise ConfigError(
                MODEL_LICENCE_BLOCKED,
                f"The model {key!r} is not on the allow-list of models reviewed for commercial use "
                f"({LICENCES_RELATIVE_PATH.as_posix()}), so it is not loaded.",
                path=path,
            )


_CACHE: dict[tuple[Path, str], ModelLicences] = {}


def _licences_path(root: Path | None) -> Path:
    own = config_root(root) / LICENCES_RELATIVE_PATH
    return own if own.is_file() else DEFAULT_CONFIG_ROOT / LICENCES_RELATIVE_PATH


def load_model_licences(root: Path | None = None) -> ModelLicences:
    """The allow-list of `root`, else the shipped one; cached per file and file content."""
    path = _licences_path(root)
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    cached = _CACHE.get((path, digest))
    if cached is not None:
        return cached
    data = load_yaml(path)
    try:
        loaded = ModelLicences.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = ".".join(str(part) for part in first["loc"]) or "model_licences"
        raise ConfigError(
            "CONFIG_INVALID", f"{LICENCES_RELATIVE_PATH.as_posix()}: {dotted}: {first['msg']}.", path=dotted
        ) from exc
    if loaded.schema_version != SCHEMA_VERSION:
        raise ConfigError(
            "CONFIG_INVALID",
            f"{LICENCES_RELATIVE_PATH.as_posix()} has schema_version {loaded.schema_version}; "
            f"this engine reads {SCHEMA_VERSION}.",
            path="schema_version",
        )
    _CACHE[(path, digest)] = loaded
    return loaded


def validate_model_licences(config: UseCaseConfig, root: Path | None = None) -> None:
    """Refuse a configuration whose strategy or selected algorithms would load a blocked model.

    Called in place by `engine.config.load_use_case` and `resolve_config`. Pure: reads the allow-list
    and the catalog, imports no model library.
    """
    licences = load_model_licences(root)
    strategy = config.model_search.strategy
    catalog = config.catalog
    licences.check_preset(catalog.strategy_presets[strategy], strategy=strategy.value)
    for key in catalog.autogluon_hyperparameter_keys(config.model_search.candidates):
        licences.check_model_key(key, path="model_search.candidates")


def foundation_warnings(config: UseCaseConfig) -> tuple[str, ...]:
    """One plain-language warning when the run picks the opt-in `foundation` strategy, else nothing."""
    from engine.config import Strategy

    if config.model_search.strategy is Strategy.FOUNDATION:
        return (FOUNDATION_NOT_LOADED_WARNING,)
    return ()


def portfolio_model_keys(preset: str) -> tuple[str, ...]:
    """The AutoGluon model keys `preset` loads, read from the installed AutoGluon (imports it).

    Resolves the preset's aliases, then its `hyperparameters` (a named portfolio, a dict, or `None` for
    AutoGluon's default set). Used by the tests to prove every allowed preset loads only allowed models.
    """
    from autogluon.tabular.configs.hyperparameter_configs import get_hyperparameter_config
    from autogluon.tabular.configs.presets_configs import tabular_presets_alias, tabular_presets_dict

    name = tabular_presets_alias.get(preset, preset)
    if name not in tabular_presets_dict:
        raise ConfigError(
            "CONFIG_INVALID",
            f"The installed AutoGluon has no preset {preset!r}.",
            path="catalog.strategy_presets",
        )
    portfolio = tabular_presets_dict[name].get("hyperparameters")
    if portfolio is None:
        portfolio = "default"
    if isinstance(portfolio, str):
        portfolio = get_hyperparameter_config(portfolio)
    return tuple(str(key) for key in portfolio)
