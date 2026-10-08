"""A channel's consent and contactable columns are never a model input (Plan J M100 part B, DEC-1310).

M99 (DEC-1309) let a use case name, per channel, the column saying the customer agreed to be contacted
on it and the column saying they can be (`actions.suppression.channels`). The use case's one consent
column (`governance.consent_column`) is reserved from the features by Phase 1's prepare, but these were
not: a training file that carried `sms_opt_in` trained on it, so a model could learn "opted in to SMS"
as a reason to rank a customer, and a scoring file then had to carry it as a model input.

**How.** The same path a user's own exclusion takes: `prepare.exclude_columns`, which Phase 1's prepare
drops (`user_excluded`), the feature schema leaves out and the uplift data preparation reserves. A
training run whose use case configures channels reads a copy of its configuration with the channel
columns added there (:func:`reserve_channel_columns`), installed on the train flow's stage table by
:func:`install_channel_column_reservation` from `engine.pipeline`'s PLAN-J block - no Phase 1 stage file
is edited (the frozen `train.py`, `evaluate.py` and `explain.py` least of all). A column the
configuration already uses for something else (the consent column, the target, a key) is not added: it
is not a feature already, and excluding it would drop it before the rule that needs it.

`run_config.json` is written by the API before the run and is not changed: the exclusion follows from
the channels it records. With no channels configured the configuration is returned as it is (the same
object) and the stage table is untouched, so a default run is byte for byte what it was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.config import UseCaseConfig

__all__ = ["channel_columns", "install_channel_column_reservation", "reserve_channel_columns"]

_LOGGER = get_logger(__name__)

_INSTALLED: Final[str] = "_channel_columns_reserved"


def channel_columns(config: UseCaseConfig) -> tuple[str, ...]:
    """Every consent and contactable column `actions.suppression.channels` names, in order, once each."""
    names: list[str] = []
    for columns in config.actions.suppression.channels.values():
        for name in (columns.consent_column, columns.contactable_column):
            if name is not None and name not in names:
                names.append(name)
    return tuple(names)


def reserve_channel_columns(config: UseCaseConfig) -> UseCaseConfig:
    """`config` with its channel columns in `prepare.exclude_columns`; `config` itself when nothing to add."""
    used = set(config._reserved_columns()) | set(config.uplift.reserved_columns())
    present = set(config.prepare.exclude_columns)
    added = tuple(name for name in channel_columns(config) if name not in used and name not in present)
    if not added:
        return config
    prepare = config.prepare.model_copy(update={"exclude_columns": (*config.prepare.exclude_columns, *added)})
    return config.model_copy(update={"prepare": prepare})


def install_channel_column_reservation(train_flow: type[Any]) -> None:
    """Wrap `train_flow._bodies` so a training run with channels reads the reserved configuration.

    Idempotent. The propensity and the uplift training flows share the stage table, so one rebinding
    covers both. The context is replaced before any stage body runs, so validation, prepare, the
    feature schema and the model card all read the same configuration.
    """
    if getattr(train_flow, _INSTALLED, False):
        return
    original: Callable[[Any], tuple[tuple[Any, Callable[[], Any]], ...]] = train_flow._bodies

    def _bodies(self: Any) -> tuple[tuple[Any, Callable[[], Any]], ...]:
        ctx = getattr(self, "_ctx", None)
        config = getattr(ctx, "config", None)
        if ctx is not None and config is not None and config.actions.suppression.channels:
            reserved = reserve_channel_columns(config)
            if reserved is not config:
                from dataclasses import replace

                self._ctx = replace(ctx, config=reserved)
                added = reserved.prepare.exclude_columns[len(config.prepare.exclude_columns) :]
                _LOGGER.info("train: %d channel column(s) left out of the model inputs", len(added))
        return original(self)

    train_flow._bodies = _bodies
    setattr(train_flow, _INSTALLED, True)
