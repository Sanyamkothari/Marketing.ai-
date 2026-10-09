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

**Models trained before this.** A model trained when the use case already configured channels (M99) may
hold a channel column among its inputs. A scoring run checks, before it predicts, the model's feature
schema (`schema.json`, the columns it was fitted on) against the channel columns the run's use case names
(:func:`install_channel_column_guard`): when any is a model input the run stops with
`CHANNEL_COLUMN_MODEL_INPUT` and says to retrain, rather than rank customers by whether they agreed to
be contacted. A run whose use case configures no channels does not check, and is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from engine.errors import EngineError
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from engine.config import UseCaseConfig

__all__ = [
    "CHANNEL_COLUMN_CODES",
    "CHANNEL_COLUMN_MODEL_INPUT",
    "channel_columns",
    "check_model_inputs",
    "install_channel_column_guard",
    "install_channel_column_reservation",
    "reserve_channel_columns",
]

_LOGGER = get_logger(__name__)

_INSTALLED: Final[str] = "_channel_columns_reserved"
_GUARDED: Final[str] = "_channel_columns_guarded"

CHANNEL_COLUMN_MODEL_INPUT: Final[str] = "CHANNEL_COLUMN_MODEL_INPUT"
"""A scoring run's model uses a channel's consent or contactable column as an input."""
CHANNEL_COLUMN_CODES: Final[frozenset[str]] = frozenset({CHANNEL_COLUMN_MODEL_INPUT})
"""The code this module defines, for `engine.decide.codes.PLAN_J_CODES` (joined at integration)."""


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


def check_model_inputs(config: UseCaseConfig, features: Iterable[str], *, model: str) -> None:
    """Refuse a model one of whose inputs is a channel column of `config` (`CHANNEL_COLUMN_MODEL_INPUT`).

    `features` are the columns the model was fitted on (its `schema.json`); `model` names it in the
    message. Does nothing when the use case configures no channels or no channel column is an input."""
    names = channel_columns(config)
    if not names:
        return
    fitted = set(features)
    used = [name for name in names if name in fitted]
    if not used:
        return
    listed = ", ".join(used)
    raise EngineError(
        CHANNEL_COLUMN_MODEL_INPUT,
        f"Model {model} was trained with {listed} as an input, and this use case uses "
        f"{'that column' if len(used) == 1 else 'those columns'} to record whether a customer agreed to be "
        "contacted on a channel. Whether someone agreed to be contacted must not decide how they are "
        "ranked, so this model cannot score for it.",
        suggestion=(
            "Train the model again: training now leaves channel consent and contactable columns out of "
            "the model inputs. Then score with the new version."
        ),
    )


def install_channel_column_guard(score_flow: type[Any]) -> None:
    """Wrap `score_flow._bodies` so a scoring run with channels checks its model's inputs before predicting.

    Idempotent. The propensity and the uplift score flows share the stage table. A run whose use case
    configures no channels gets its stage table back untouched."""
    if getattr(score_flow, _GUARDED, False):
        return
    original: Callable[[Any], tuple[tuple[Any, Callable[[], Any]], ...]] = score_flow._bodies

    def _bodies(self: Any) -> tuple[tuple[Any, Callable[[], Any]], ...]:
        bodies = original(self)
        config = getattr(getattr(self, "_ctx", None), "config", None)
        if config is None or not channel_columns(config):
            return bodies
        return tuple((key, _checked(self, key, body)) for key, body in bodies)

    score_flow._bodies = _bodies
    setattr(score_flow, _GUARDED, True)


def _checked(flow: Any, key: Any, body: Callable[[], Any]) -> Callable[[], Any]:
    from functools import wraps

    from engine.contracts import StageKey

    if key is not StageKey.PREDICT:
        return body

    @wraps(body)
    def predict() -> Any:
        from engine.contracts import FeatureSchema
        from engine.storage import StorageError

        version = getattr(flow, "_version", None)
        if version is not None:
            try:
                schema = flow._storage.read_model(version.schema_key, FeatureSchema)
            except (StorageError, ValueError):  # the schema check before this stage read it; not ours
                _LOGGER.warning("channel columns: the model's schema could not be read")
            else:
                check_model_inputs(
                    flow._ctx.config, (column.name for column in schema.columns), model=version.model_id
                )
        return body()

    return predict
